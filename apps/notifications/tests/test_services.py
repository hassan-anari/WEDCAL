# mypy: disable-error-code="no-untyped-def"
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.notifications.models import (
    BroadcastCampaign,
    DeliveryState,
    NotificationTemplate,
    TelegramDelivery,
)
from apps.notifications.services import (
    NotificationIdempotencyConflict,
    deliver_notifications,
    emit_notification,
    materialize_campaign,
    queue_campaign,
)
from apps.players.models import Player
from apps.telegram.bot_api import TelegramResult

pytestmark = pytest.mark.django_db(transaction=True)


def player(user_id: int = 100, reachable: bool = True) -> Player:
    now = timezone.now()
    return Player.objects.create(
        telegram_user_id=user_id,
        first_seen_at=now,
        first_seen_source=Player.FirstSeenSource.BOT_START,
        first_private_contact_at=now if reachable else None,
        last_seen_at=now,
    )


def test_notification_idempotency_and_conflict():
    p = player()
    first, created = emit_notification(
        recipient=p,
        event_key="TEST",
        source_domain="TEST",
        source_type="ROW",
        source_id="1",
        idempotency_key="same",
        payload={"safe": 1},
    )
    replay, replay_created = emit_notification(
        recipient=p,
        event_key="TEST",
        source_domain="TEST",
        source_type="ROW",
        source_id="1",
        idempotency_key="same",
        payload={"safe": 1},
    )
    assert created and not replay_created and replay.pk == first.pk
    with pytest.raises(NotificationIdempotencyConflict):
        emit_notification(
            recipient=p,
            event_key="OTHER",
            source_domain="TEST",
            source_type="ROW",
            source_id="1",
            idempotency_key="same",
        )


def test_private_contact_unavailable_keeps_in_app_notification():
    p = player(reachable=False)
    notification, _ = emit_notification(
        recipient=p,
        event_key="TEST",
        source_domain="TEST",
        source_type="ROW",
        source_id="1",
        idempotency_key="unreachable",
        telegram=True,
    )
    assert notification.telegram_delivery.state == DeliveryState.SKIPPED


def test_telegram_429_schedules_retry_without_sleep(monkeypatch):
    p = player()
    staff = User.objects.create_user("staff")
    template = NotificationTemplate.objects.create(
        event_key="TEST",
        channel=NotificationTemplate.Channel.TELEGRAM,
        version=1,
        body="configured",
        created_by=staff,
    )
    assert template.pk
    notification, _ = emit_notification(
        recipient=p,
        event_key="TEST",
        source_domain="TEST",
        source_type="ROW",
        source_id="1",
        idempotency_key="rate",
        telegram=True,
    )
    monkeypatch.setattr(
        "apps.notifications.services.bot_api.send_message",
        lambda *_: TelegramResult(False, category="rate_limited", retry_after=17),
    )
    before = timezone.now()
    assert deliver_notifications() == 1
    delivery = TelegramDelivery.objects.get(notification=notification)
    assert delivery.state == DeliveryState.RETRY_WAIT
    assert delivery.next_attempt_at and delivery.next_attempt_at >= before + timedelta(seconds=17)


def test_ambiguous_send_is_uncertain_and_not_automatically_retried(monkeypatch):
    p = player()
    staff = User.objects.create_user("staff")
    NotificationTemplate.objects.create(
        event_key="TEST",
        channel=NotificationTemplate.Channel.TELEGRAM,
        version=1,
        body="configured",
        created_by=staff,
    )
    notification, _ = emit_notification(
        recipient=p,
        event_key="TEST",
        source_domain="TEST",
        source_type="ROW",
        source_id="1",
        idempotency_key="ambiguous",
        telegram=True,
    )
    calls = 0

    def ambiguous(*args):
        nonlocal calls
        calls += 1
        return TelegramResult(False, category="timeout", ambiguous=True)

    monkeypatch.setattr("apps.notifications.services.bot_api.send_message", ambiguous)
    deliver_notifications()
    deliver_notifications()
    delivery = TelegramDelivery.objects.get(notification=notification)
    assert delivery.state == DeliveryState.UNCERTAIN and calls == 1


def test_broadcast_materializes_stable_unique_audience():
    staff = User.objects.create_user("staff")
    reachable, unreachable = player(101), player(102, False)
    campaign = BroadcastCampaign.objects.create(
        audience_type=BroadcastCampaign.Audience.TELEGRAM_REACHABLE,
        body_snapshot="configured",
        created_by=staff,
        purpose="operations",
    )
    queue_campaign(campaign=campaign, actor=staff, reason="operations")
    assert materialize_campaign(campaign.pk, batch_size=100) == 1
    assert list(campaign.recipients.values_list("player_id", flat=True)) == [reachable.pk]
    assert unreachable.pk != reachable.pk
    assert materialize_campaign(campaign.pk, batch_size=100) == 0
