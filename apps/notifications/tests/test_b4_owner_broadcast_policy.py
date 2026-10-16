# ruff: noqa: F811
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
"""Owner policy correction; all transports are synthetic and real HTTP is forbidden."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, close_old_connections, transaction
from django.utils import timezone

from apps.network.models import CreatorGroupRegistration
from apps.notifications.audiences import eligible_groups, eligible_users, estimated_targets
from apps.notifications.models import (
    BroadcastRecipient,
    GroupBroadcastSubscription,
    OperatorAuditEvent,
)
from apps.notifications.services import (
    audience_metrics,
    deliver_broadcasts,
    materialize_campaign,
    queue_campaign,
)
from apps.notifications.tests.test_b3_audiences import audience_world, campaign  # noqa: F401
from apps.notifications.tests.test_services import player
from apps.telegram.bot_api import TelegramResult
from apps.telegram.models import TelegramGroup
from apps.telegram.tests.test_b3_budget import sending_budget  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def forbid_external_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Policy tests must never contact Telegram or real recipients")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)


def test_all_known_admin_groups_legacy_registered_and_unrelated_need_no_preference(
    audience_world, monkeypatch
):
    actor, creator, group = audience_world
    now = timezone.now()
    registered = TelegramGroup.objects.create(
        telegram_chat_id=-200,
        chat_type="group",
        bot_membership_status="administrator",
        bot_is_admin=True,
        first_seen_at=now - timedelta(days=100),
        last_seen_at=now,
    )
    CreatorGroupRegistration.objects.create(group=registered, creator=creator)
    legacy = GroupBroadcastSubscription.objects.create(
        group=registered,
        enabled=False,
        revoked_at=now,
        revision=4,
        authorized_telegram_user_id=creator.telegram_user_id,
    )
    original = GroupBroadcastSubscription.objects.filter(pk=legacy.pk).values().get()
    assert not CreatorGroupRegistration.objects.filter(group=group).exists()
    row = campaign(actor, "BOTH")
    queue_campaign(campaign=row, actor=actor, reason="Explicit operator confirmation")
    assert materialize_campaign(row.pk) == 3
    assert estimated_targets("BOTH") == {"users": 1, "groups": 2}
    assert row.recipients.filter(group_subscription__isnull=False).count() == 0
    sent = []

    def send(chat, text):
        sent.append(chat)
        return TelegramResult(True)

    monkeypatch.setattr("apps.telegram.bot_api.send_message", send)

    deliver_broadcasts()
    deliver_broadcasts()
    assert sorted(sent) == [-200, -100, creator.telegram_user_id]
    assert row.recipients.filter(state="SENT").count() == 3
    assert GroupBroadcastSubscription.objects.filter(pk=legacy.pk).values().get() == original
    assert GroupBroadcastSubscription.objects.count() == 1
    assert (
        OperatorAuditEvent.objects.get(action="BROADCAST_QUEUE").after_state[
            "audience_policy_version"
        ]
        == 2
    )
    if os.getenv("B4_EXPORT_FIXTURES"):
        Path("docs/fixtures/b4/broadcast-policy.json").write_text(
            json.dumps(
                {
                    "policy_version": 2,
                    "provenance": "isolated PostgreSQL; synthetic Bot API only",
                    "estimates": estimated_targets("BOTH"),
                    "metrics": audience_metrics(row),
                    "targets": list(
                        row.recipients.values(
                            "target_kind", "telegram_user_id", "state", "group_subscription_id"
                        )
                    ),
                    "legacy_preference_unchanged": True,
                },
                indent=2,
            )
            + "\n"
        )


@pytest.mark.parametrize(
    "loss",
    [
        "left",
        "kicked",
        "member",
        "restricted",
        "wrong_identity",
        "cannot_send",
        "forbidden",
        "chat_not_found",
        "event_during_check",
    ],
)
def test_permission_loss_or_forbidden_skips_group_without_any_message(
    audience_world, monkeypatch, loss
):
    actor, _, group = audience_world
    row = campaign(actor, "GROUPS")
    queue_campaign(campaign=row, actor=actor, reason="Confirm")
    materialize_campaign(row.pk)
    calls = []

    def member(chat, user):
        if loss in {"forbidden", "chat_not_found"}:
            return TelegramResult(False, category=loss)
        if loss == "event_during_check":
            TelegramGroup.objects.filter(pk=group.pk).update(
                bot_membership_status="left",
                bot_is_admin=False,
                bot_membership_last_update_id=100,
            )
        return TelegramResult(
            True,
            {
                "status": loss
                if loss in {"left", "kicked", "member", "restricted"}
                else "administrator",
                "user": {"id": user + 1 if loss == "wrong_identity" else user},
                "can_send_messages": loss != "cannot_send",
            },
        )

    monkeypatch.setattr("apps.telegram.bot_api.get_chat_member", member)
    monkeypatch.setattr("apps.telegram.bot_api.send_message", lambda *args: calls.append(args))
    deliver_broadcasts()
    assert row.recipients.get().state == "SKIPPED" and not calls


@pytest.mark.parametrize("loss", ["left", "kicked", "member", "restricted"])
def test_observed_membership_loss_excludes_future_campaigns(audience_world, loss):
    actor, _, group = audience_world
    TelegramGroup.objects.filter(pk=group.pk).update(bot_membership_status=loss, bot_is_admin=False)
    assert not eligible_groups().exists()
    row = campaign(actor, "GROUPS")
    queue_campaign(campaign=row, actor=actor, reason="Confirm")
    assert materialize_campaign(row.pk) == 0


def test_private_contacts_only_and_blocked_target_does_not_disrupt_other_recipients(
    audience_world, monkeypatch
):
    actor, contacted, group = audience_world
    never = player(103, reachable=False)
    blocked = player(104)
    moderated = player(105)
    moderated.moderation_state = "SUSPENDED"
    moderated.save(update_fields=["moderation_state"])
    # Messaging eligibility does not alter gameplay moderation or Creator economic rules.
    assert eligible_users().filter(pk=moderated.pk).exists()
    row = campaign(actor, "BOTH")
    queue_campaign(campaign=row, actor=actor, reason="Confirm mixed audience")
    materialize_campaign(row.pk)
    assert not row.recipients.filter(player=never).exists()
    sent = []

    def send(chat, text):
        sent.append(chat)
        return (
            TelegramResult(False, category="forbidden")
            if chat == blocked.telegram_user_id
            else TelegramResult(True)
        )

    monkeypatch.setattr("apps.telegram.bot_api.send_message", send)
    deliver_broadcasts()
    deliver_broadcasts()
    assert row.recipients.get(player=blocked).state == "SKIPPED"
    assert row.recipients.exclude(player=blocked).filter(state="SENT").count() == 3
    assert len(sent) == len(set(sent)) == 4
    moderated.refresh_from_db()
    assert moderated.moderation_state == "SUSPENDED"


def test_group_block_after_preflight_is_terminal_skip(audience_world, monkeypatch):
    actor, _, group = audience_world
    row = campaign(actor, "GROUPS")
    queue_campaign(campaign=row, actor=actor, reason="Confirm")
    materialize_campaign(row.pk)
    monkeypatch.setattr(
        "apps.telegram.bot_api.send_message",
        lambda *args: TelegramResult(False, category="forbidden"),
    )
    deliver_broadcasts()
    deliver_broadcasts()
    target = row.recipients.get()
    assert target.state == "SKIPPED" and target.attempt_count == 1


def test_direct_targets_concurrent_materialization_and_chat_uniqueness(audience_world):
    actor, _, group = audience_world
    row = campaign(actor, "BOTH")
    queue_campaign(campaign=row, actor=actor, reason="Confirm")

    def run(_):
        close_old_connections()
        try:
            return materialize_campaign(row.pk, batch_size=1)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    assert row.recipients.count() == 2
    legacy = GroupBroadcastSubscription.objects.create(group=group, authorized_telegram_user_id=123)
    with pytest.raises(DatabaseError), transaction.atomic():
        BroadcastRecipient.objects.create(
            campaign=row,
            group_subscription=legacy,
            target_kind="GROUP",
            telegram_user_id=group.telegram_chat_id,
        )


def test_legacy_campaign_materialization_needs_new_confirmation_and_policy_is_immutable(
    audience_world,
):
    from apps.notifications.models import BroadcastCampaign

    actor, _, _ = audience_world
    row = campaign(actor)
    BroadcastCampaign.objects.filter(pk=row.pk).update(audience_policy_version=1)
    row = queue_campaign(campaign=row, actor=actor, reason="New operator confirmation")
    assert row.audience_policy_version == 2
    with pytest.raises(DatabaseError), transaction.atomic():
        BroadcastCampaign.objects.filter(pk=row.pk).update(audience_policy_version=1)
    old = campaign(actor)
    BroadcastCampaign.objects.filter(pk=old.pk).update(
        state="MATERIALIZING", audience_policy_version=1
    )
    assert materialize_campaign(old.pk) == 0 and not old.recipients.exists()


def test_policy_does_not_allow_unconfirmed_or_unauthorized_campaign(audience_world):
    from django.contrib.auth.models import User

    actor, _, _ = audience_world
    row = campaign(actor)
    assert materialize_campaign(row.pk) == 0 and not row.recipients.exists()
    with pytest.raises(PermissionDenied):
        queue_campaign(campaign=row, actor=User.objects.create_user("not-operator"), reason="No")


def test_mixed_delivery_honors_real_send_budget_and_retries_without_duplicate(
    audience_world, sending_budget, settings, monkeypatch
):
    from types import SimpleNamespace

    from apps.telegram import bot_api, budget

    actor, _, _ = audience_world
    player(106)
    settings.TELEGRAM_BOT_TOKEN = "1:isolated-policy"
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b'{"ok":true,"result":{"message_id":42}}'

    def transport(request, **kwargs):
        assert request.full_url == "https://api.telegram.org/bot1:isolated-policy/sendMessage"
        calls.append(json.loads(request.data)["chat_id"])
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", transport)
    monkeypatch.setattr(
        bot_api,
        "send_message",
        lambda chat, text: bot_api.request("sendMessage", {"chat_id": chat, "text": text}),
    )
    row = campaign(actor, "BOTH")
    queue_campaign(campaign=row, actor=actor, reason="Explicit mixed confirmation")
    materialize_campaign(row.pk)
    deliver_broadcasts()
    assert row.recipients.filter(state="SENT").count() == 2
    waiting = row.recipients.get(state="RETRY_WAIT")
    assert waiting.error_category == "local_rate_limited" and waiting.attempt_count == 0
    assert budget.reserve_bot_api_budget("getChatMember") is None
    monkeypatch.setattr(budget, "time", SimpleNamespace(time=lambda: 1004))
    row.recipients.filter(pk=waiting.pk).update(
        next_attempt_at=timezone.now() - timedelta(seconds=1)
    )
    deliver_broadcasts()
    deliver_broadcasts()
    assert row.recipients.filter(state="SENT").count() == 3
    assert len(calls) == len(set(calls)) == 3


def test_telegram_membership_updates_discover_and_remove_non_creator_audience(audience_world):
    from apps.telegram.services import persist_inbound_update, process_inbound_update

    actor, _, _ = audience_world

    def observed(update_id, status):
        inbound, _ = persist_inbound_update(
            {
                "update_id": update_id,
                "my_chat_member": {
                    "date": int(timezone.now().timestamp()),
                    "chat": {"id": -500, "type": "supergroup", "title": "Observed only"},
                    "from": {"id": 123, "is_bot": False, "first_name": "Admin"},
                    "old_chat_member": {"status": "member", "user": {"id": 999, "is_bot": True}},
                    "new_chat_member": {"status": status, "user": {"id": 999, "is_bot": True}},
                },
            },
            authenticated=True,
        )
        process_inbound_update(inbound.pk)

    observed(8001, "administrator")
    group = TelegramGroup.objects.get(telegram_chat_id=-500)
    assert eligible_groups().filter(pk=group.pk).exists()
    assert not CreatorGroupRegistration.objects.filter(group=group).exists()
    observed(8002, "kicked")
    observed(8001, "administrator")  # Replay cannot restore removed membership.
    assert not eligible_groups().filter(pk=group.pk).exists()
