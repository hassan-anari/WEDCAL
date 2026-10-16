# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import close_old_connections
from django.utils import timezone

from apps.notifications.audiences import set_group_consent
from apps.notifications.models import (
    BroadcastCampaign,
    BroadcastRecipient,
    DeliveryState,
    GroupBroadcastSubscription,
)
from apps.notifications.services import (
    audience_metrics,
    cancel_campaign,
    claim_broadcast_recipients,
    deliver_broadcasts,
    materialize_campaign,
    queue_campaign,
)
from apps.notifications.tests.test_services import authorized_operator, player
from apps.telegram.bot_api import TelegramResult
from apps.telegram.models import TelegramGroup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def audience_world(monkeypatch, settings):
    settings.TELEGRAM_BOT_ID = 999
    settings.B3_BROADCAST_AUDIENCES_ENABLED = True

    def member(chat, user):
        return TelegramResult(True, {"status": "administrator", "user": {"id": user}})

    monkeypatch.setattr("apps.telegram.bot_api.get_chat_member", member)
    monkeypatch.setattr(
        "apps.telegram.bot_api.send_message", lambda *_: TelegramResult(True, {"message_id": 42})
    )
    now = timezone.now()
    actor = authorized_operator("audience-operator")
    p = player()
    player(102, reachable=False)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-100,
        chat_type="supergroup",
        title="Synthetic community",
        first_seen_at=now,
        last_seen_at=now,
        bot_membership_status="administrator",
        bot_is_admin=True,
    )
    return actor, p, group


def campaign(actor, audience="BOTH"):
    return BroadcastCampaign.objects.create(
        created_by=actor,
        audience_type=audience,
        body_snapshot="Synthetic fixture broadcast",
        purpose="tests only",
    )


def test_observed_groups_never_enroll_and_consent_revocation_prevents_dispatch(audience_world):
    actor, p, group = audience_world
    row = campaign(actor)
    queue_campaign(campaign=row, actor=actor, reason="synthetic")
    assert materialize_campaign(row.pk) == 1
    assert not row.recipients.filter(target_kind="GROUP").exists()
    assert not GroupBroadcastSubscription.objects.exists()
    set_group_consent(group, p.telegram_user_id, enabled=True)
    row2 = campaign(actor)
    queue_campaign(campaign=row2, actor=actor, reason="synthetic")
    assert materialize_campaign(row2.pk) == 2
    set_group_consent(group, p.telegram_user_id, enabled=False)
    deliver_broadcasts()
    deliver_broadcasts()
    assert row2.recipients.get(target_kind="GROUP").state == DeliveryState.SKIPPED
    assert row2.recipients.get(target_kind="USER").state == DeliveryState.SENT


@pytest.mark.parametrize(
    "audience,users,groups", [("USERS", 1, 0), ("GROUPS", 0, 1), ("BOTH", 1, 1)]
)
def test_bounded_restart_safe_materialization_and_independent_metrics(
    audience_world, audience, users, groups
):
    actor, p, group = audience_world
    set_group_consent(group, p.telegram_user_id, enabled=True)
    row = campaign(actor, audience)
    queue_campaign(campaign=row, actor=actor, reason="synthetic")
    for _ in range(4):
        materialize_campaign(row.pk, batch_size=1)
    assert row.recipients.filter(target_kind="USER").count() == users
    assert row.recipients.filter(target_kind="GROUP").count() == groups
    deliver_broadcasts()
    row.refresh_from_db()
    report = audience_metrics(row)
    assert report["users"]["sent"] == users and report["groups"]["sent"] == groups
    assert report["estimated_users"] == users and report["estimated_groups"] == groups
    assert row.recipients.filter(target_kind="GROUP", player__isnull=False).count() == 0
    if os.getenv("B3_EXPORT_FIXTURES") and audience == "BOTH":
        Path("docs/fixtures/b3/broadcast-audiences.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )


@pytest.mark.parametrize("category", ["local_rate_limited", "rate_limited", "budget_unavailable"])
def test_user_and_group_temporary_throttling_never_permanently_fails(
    audience_world, monkeypatch, settings, category
):
    actor, p, group = audience_world
    set_group_consent(group, p.telegram_user_id, enabled=True)
    row = campaign(actor)
    queue_campaign(campaign=row, actor=actor, reason="synthetic")
    materialize_campaign(row.pk)
    row.recipients.update(attempt_count=settings.NOTIFICATION_MAX_ATTEMPTS)
    monkeypatch.setattr(
        "apps.telegram.bot_api.send_message",
        lambda *_: TelegramResult(False, category=category, retry_after=17),
    )
    before = timezone.now()
    deliver_broadcasts()
    assert row.recipients.filter(state=DeliveryState.RETRY_WAIT).count() == 2
    assert not row.recipients.filter(next_attempt_at__lt=before + timedelta(seconds=17)).exists()
    if os.getenv("B3_EXPORT_FIXTURES"):
        report = {
            "provenance": "Synthetic PostgreSQL test with mocked Telegram response",
            "telegram_result": {"ok": False, "category": category, "retry_after": 17},
            "metrics": audience_metrics(row),
            "targets": list(
                row.recipients.values(
                    "target_kind", "state", "error_category", "attempt_count", "next_attempt_at"
                )
            ),
        }
        Path(f"docs/fixtures/b3/broadcast-retry-{category}.json").write_text(
            json.dumps(report, default=str, indent=2) + "\n"
        )


def test_group_loses_send_permission_and_reconsent_does_not_revive_old_target(
    audience_world, monkeypatch
):
    actor, p, group = audience_world
    set_group_consent(group, p.telegram_user_id, enabled=True)
    row = campaign(actor, "GROUPS")
    queue_campaign(campaign=row, actor=actor, reason="synthetic")
    materialize_campaign(row.pk)
    monkeypatch.setattr(
        "apps.telegram.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "left", "user": {"id": 999}}),
    )
    deliver_broadcasts()
    assert row.recipients.get().state == DeliveryState.SKIPPED


def test_cancel_and_expired_dispatch_lease_do_not_resend(audience_world):
    actor, p, group = audience_world
    set_group_consent(group, p.telegram_user_id, enabled=True)
    row = campaign(actor)
    queue_campaign(campaign=row, actor=actor, reason="synthetic")
    materialize_campaign(row.pk)
    claimed = claim_broadcast_recipients(1)
    BroadcastRecipient.objects.filter(pk=claimed[0].pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )
    cancel_campaign(campaign=row, actor=actor, reason="cancel")
    from apps.notifications.services import reconcile_campaign

    reconcile_campaign(row.pk)
    assert row.recipients.filter(state=DeliveryState.UNCERTAIN).count() == 1
    assert row.recipients.filter(state=DeliveryState.SKIPPED).count() == 1
    assert deliver_broadcasts() == 0
    if os.getenv("B3_EXPORT_FIXTURES"):
        row.refresh_from_db()
        Path("docs/fixtures/b3/broadcast-cancel-uncertain.json").write_text(
            json.dumps(audience_metrics(row), indent=2) + "\n"
        )


def test_parallel_materializers_never_duplicate_targets(audience_world):
    actor, p, group = audience_world
    set_group_consent(group, p.telegram_user_id, enabled=True)
    row = campaign(actor)
    queue_campaign(campaign=row, actor=actor, reason="parallel")

    def run(_):
        close_old_connections()
        try:
            return materialize_campaign(row.pk, batch_size=1)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    assert row.recipients.count() == 2


@pytest.mark.parametrize(
    "defect",
    [
        "non_admin",
        "wrong_identity",
        "bot_left",
        "not_a_group",
        "no_send_permission",
        "telegram_error",
    ],
)
def test_group_opt_in_requires_real_authority_and_send_permission(
    audience_world, monkeypatch, defect
):
    from django.core.exceptions import ValidationError

    actor, p, group = audience_world

    def member(chat, user):
        if defect == "telegram_error":
            return TelegramResult(False, category="rate_limited", retry_after=3)
        status = "administrator"
        returned = user
        if defect == "non_admin" and user == p.telegram_user_id:
            status = "member"
        if defect == "wrong_identity" and user == p.telegram_user_id:
            returned = user + 1
        if defect == "bot_left" and user == 999:
            status = "left"
        if defect == "no_send_permission" and user == 999:
            status = "member"
        return TelegramResult(True, {"status": status, "user": {"id": returned}})

    monkeypatch.setattr("apps.telegram.bot_api.get_chat_member", member)
    monkeypatch.setattr(
        "apps.telegram.bot_api.request",
        lambda *_: TelegramResult(True, {"permissions": {"can_send_messages": False}}),
    )
    if defect == "not_a_group":
        group.telegram_chat_id = 100
    with pytest.raises(ValidationError):
        set_group_consent(group, p.telegram_user_id, enabled=True)
    assert not GroupBroadcastSubscription.objects.exists()
