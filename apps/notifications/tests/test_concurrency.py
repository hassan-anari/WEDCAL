# mypy: disable-error-code="no-untyped-call,no-untyped-def"
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import close_old_connections
from django.utils import timezone

from apps.notifications.models import BroadcastCampaign, BroadcastRecipient, DeliveryState
from apps.notifications.services import (
    cancel_campaign,
    claim_broadcast_recipients,
    reconcile_campaign,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db(transaction=True)


def setup_campaign() -> tuple[BroadcastCampaign, BroadcastRecipient, User]:
    now = timezone.now()
    actor = User.objects.create_user("broadcast-race")
    player = Player.objects.create(
        telegram_user_id=880001,
        first_seen_at=now,
        first_seen_source=Player.FirstSeenSource.BOT_START,
        first_private_contact_at=now,
        last_seen_at=now,
    )
    campaign = BroadcastCampaign.objects.create(
        state=BroadcastCampaign.State.QUEUED,
        audience_type=BroadcastCampaign.Audience.TELEGRAM_REACHABLE,
        body_snapshot="configured",
        created_by=actor,
        purpose="race",
    )
    recipient = BroadcastRecipient.objects.create(
        campaign=campaign, player=player, telegram_user_id=player.telegram_user_id
    )
    return campaign, recipient, actor


def test_two_workers_never_own_same_recipient_lease():
    _, recipient, _ = setup_campaign()

    def claim():
        close_old_connections()
        rows = claim_broadcast_recipients(1)
        result = [(row.pk, row.lease_token) for row in rows]
        close_old_connections()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    claims = [item for result in results for item in result]
    assert len(claims) == 1 and claims[0][0] == recipient.pk


def test_cancellation_vs_claim_obeys_campaign_lock_order():
    campaign, recipient, actor = setup_campaign()

    def claim():
        close_old_connections()
        result = claim_broadcast_recipients(1)
        close_old_connections()
        return bool(result)

    def cancel():
        close_old_connections()
        cancel_campaign(
            campaign=BroadcastCampaign.objects.get(pk=campaign.pk),
            actor=User.objects.get(pk=actor.pk),
            reason="race cancellation",
        )
        close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed_future = pool.submit(claim)
        cancelled_future = pool.submit(cancel)
        claimed = claimed_future.result()
        cancelled_future.result()
    campaign.refresh_from_db()
    recipient.refresh_from_db()
    assert campaign.state == BroadcastCampaign.State.CANCELLED
    if claimed:
        assert recipient.state == DeliveryState.PROCESSING
        BroadcastRecipient.objects.filter(pk=recipient.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        reconcile_campaign(campaign.pk)
        recipient.refresh_from_db()
        assert recipient.state == DeliveryState.UNCERTAIN
        assert claim_broadcast_recipients(1) == []
    else:
        assert recipient.state == DeliveryState.SKIPPED


def test_expired_lease_reclaim_vs_cancellation_converges():
    campaign, recipient, actor = setup_campaign()
    BroadcastRecipient.objects.filter(pk=recipient.pk).update(
        state=DeliveryState.PROCESSING,
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    def reclaim():
        close_old_connections()
        claimed = bool(claim_broadcast_recipients(1))
        close_old_connections()
        return claimed

    def cancel():
        close_old_connections()
        cancel_campaign(
            campaign=BroadcastCampaign.objects.get(pk=campaign.pk),
            actor=User.objects.get(pk=actor.pk),
            reason="expired lease race",
        )
        close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reclaimed_future = pool.submit(reclaim)
        cancelled_future = pool.submit(cancel)
        reclaimed = reclaimed_future.result()
        cancelled_future.result()
    reconcile_campaign(campaign.pk)
    recipient.refresh_from_db()
    assert recipient.state in (
        DeliveryState.PROCESSING if reclaimed else DeliveryState.UNCERTAIN,
        DeliveryState.UNCERTAIN,
    )


def test_final_inflight_terminalization_vs_cancel_converges(monkeypatch):
    from apps.notifications import services
    from apps.telegram.bot_api import TelegramResult

    campaign, recipient, actor = setup_campaign()
    claimed = claim_broadcast_recipients(1)[0]
    monkeypatch.setattr(
        "apps.notifications.services.bot_api.send_message",
        lambda *_: TelegramResult(True, result={"message_id": 99}),
    )

    def terminalize():
        close_old_connections()
        row = BroadcastRecipient.objects.get(pk=claimed.pk)
        services._deliver(row, "configured")
        reconcile_campaign(campaign.pk)
        close_old_connections()

    def cancel():
        close_old_connections()
        cancel_campaign(
            campaign=BroadcastCampaign.objects.get(pk=campaign.pk),
            actor=User.objects.get(pk=actor.pk),
            reason="final recipient race",
        )
        close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(terminalize), pool.submit(cancel))
        for future in futures:
            future.result()
    campaign.refresh_from_db()
    recipient.refresh_from_db()
    assert recipient.state == DeliveryState.SENT
    assert campaign.state in (
        BroadcastCampaign.State.COMPLETED,
        BroadcastCampaign.State.CANCELLED,
    )
