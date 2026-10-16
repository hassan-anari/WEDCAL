# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import Client
from django.utils import timezone

from apps.clans.activity import home_integrity_at
from apps.clans.economy import purchase_item, settle_clan_reward
from apps.clans.governance import (
    observe_bot_admin_state,
    record_clan_activity,
    sweep_bot_admin_deadlines,
    sweep_inactivity,
)
from apps.clans.models import (
    ClanGovernanceCase,
    ClanMembership,
    ClanMembershipEvent,
    ClanRole,
    ClanSeason,
)
from apps.clans.recovery import (
    observe_captain_authority,
    recheck_lost_captain_authority,
    recovery_reasons,
    verify_captain_authority,
)
from apps.clans.services import ClanConflict, request_join
from apps.notifications.models import Notification
from apps.telegram.bot_api import TelegramResult
from apps.telegram.models import TelegramInboundUpdate
from apps.telegram.services import process_inbound_update

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def authority_world(clan_world, monkeypatch, settings):
    cs = clan_world[-1]()
    settings.TELEGRAM_BOT_ID = 123456
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    state = {"captain": "member", "bot": "administrator", "invite": True}
    calls = []

    def get_member(chat_id, user_id):
        calls.append((chat_id, user_id))
        if state.get("unknown"):
            return TelegramResult(False, category="rate_limited", retry_after=1)
        return TelegramResult(
            True,
            result={
                "status": state["bot"] if user_id == settings.TELEGRAM_BOT_ID else state["captain"],
                "can_invite_users": state["invite"],
            },
        )

    monkeypatch.setattr("apps.telegram.bot_api.get_chat_member", get_member)
    return cs, state, calls


def test_confirmed_demotion_preserves_identity_and_notifies_once(authority_world):
    cs, _, _ = authority_world
    identity = (cs.clan_id, cs.captain_id, cs.treasury_balance, cs.hq_level)
    verify_captain_authority(cs.pk)
    verify_captain_authority(cs.pk)
    cs.refresh_from_db()
    assert cs.state == "RECOVERY" and cs.recovery_reason == "CAPTAIN_AUTHORITY_LOSS"
    assert identity == (cs.clan_id, cs.captain_id, cs.treasury_balance, cs.hq_level)
    assert cs.memberships.filter(ended_at__isnull=True, role="CAPTAIN").count() == 1
    assert not ClanGovernanceCase.objects.exists()
    assert (
        Notification.objects.filter(
            event_key="CLAN_CAPTAIN_AUTHORITY_LOST", recipient=cs.captain
        ).count()
        == 1
    )
    record_clan_activity(cs.memberships.get().pk, occurred_at=timezone.now())
    cs.refresh_from_db()
    assert cs.state == "RECOVERY" and cs.captain_authority_lost_at is not None


@pytest.mark.parametrize("role", ["creator", "administrator"])
def test_restore_requires_fresh_original_captain_and_authorized_bot(authority_world, role):
    cs, state, calls = authority_world
    verify_captain_authority(cs.pk)
    observe_captain_authority(
        cs.pk, has_authority=True, authoritative=True, observed_at=timezone.now()
    )
    cs.refresh_from_db()
    assert cs.state == "RECOVERY"  # an update alone cannot restore
    state["captain"] = role
    calls.clear()
    restored = verify_captain_authority(cs.pk)
    assert restored.state == "ACTIVE" and restored.captain_authority_lost_at is None
    assert len(calls) == 2


@pytest.mark.parametrize("bot_state,invite", [("member", False), ("administrator", False)])
def test_lost_bot_permissions_prevent_authority_restoration(authority_world, bot_state, invite):
    cs, state, _ = authority_world
    verify_captain_authority(cs.pk)
    state.update(captain="creator", bot=bot_state, invite=invite)
    cs = verify_captain_authority(cs.pk)
    assert cs.state == "RECOVERY" and cs.captain_authority_lost_at is not None
    assert set(recovery_reasons(cs)) == {"CAPTAIN_AUTHORITY_LOSS", "BOT_ADMIN_LOSS"}
    observe_bot_admin_state(cs.pk, is_admin=True, authoritative=True)
    cs.refresh_from_db()
    assert cs.state == "RECOVERY"  # bot restoration alone cannot clear Captain loss
    state.update(bot="administrator", invite=True)
    assert verify_captain_authority(cs.pk).state == "ACTIVE"


def test_overlapping_inactivity_and_bot_deadline_are_independent(authority_world):
    cs, state, _ = authority_world
    verify_captain_authority(cs.pk)
    now = timezone.now()
    captain = cs.memberships.get()
    captain.last_clan_active_at = now - timedelta(days=8)
    captain.next_inactivity_check_at = now
    captain.save()
    sweep_inactivity(now=now)
    cs.refresh_from_db()
    assert set(recovery_reasons(cs)) == {"CAPTAIN_AUTHORITY_LOSS", "CAPTAIN_INACTIVITY"}
    state["captain"] = "administrator"
    cs = verify_captain_authority(cs.pk)
    assert cs.state == "RECOVERY" and cs.recovery_reason == "CAPTAIN_INACTIVITY"
    record_clan_activity(captain.pk, occurred_at=timezone.now())
    cs.refresh_from_db()
    assert cs.state == "ACTIVE"
    state["captain"] = "member"
    verify_captain_authority(cs.pk)
    observe_bot_admin_state(cs.pk, is_admin=False, authoritative=True)
    ClanSeason.objects.filter(pk=cs.pk).update(bot_admin_restore_deadline=timezone.now())
    assert sweep_bot_admin_deadlines() == 1
    cs.refresh_from_db()
    assert cs.bot_admin_recovery_at is not None
    state["captain"] = "administrator"
    assert verify_captain_authority(cs.pk).state == "ACTIVE"


def test_unknown_or_stale_evidence_cannot_clear_or_reenter_recovery(authority_world):
    cs, state, _ = authority_world
    verify_captain_authority(cs.pk)
    state["unknown"] = True
    with pytest.raises(ClanConflict):
        verify_captain_authority(cs.pk)
    cs.refresh_from_db()
    assert cs.state == "RECOVERY"
    old = cs.captain_authority_observed_at
    state.update(unknown=False, captain="creator")
    verify_captain_authority(cs.pk)
    observe_captain_authority(cs.pk, has_authority=False, authoritative=True, observed_at=old)
    cs.refresh_from_db()
    assert cs.state == "ACTIVE"
    observe_captain_authority(
        cs.pk, has_authority=False, authoritative=False, observed_at=timezone.now()
    )
    cs.refresh_from_db()
    assert cs.state == "ACTIVE"


def test_recovery_blocks_new_mutations_but_settles_pre_loss_sources(authority_world, clan_world):
    cs, _, _ = authority_world
    now, _, _, resolved, make_player, _ = clan_world
    member = cs.memberships.get()
    ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    verify_captain_authority(cs.pk)
    cs.refresh_from_db()
    assert home_integrity_at(cs, now)
    assert not home_integrity_at(cs, timezone.now())
    settlement, created = settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="before-authority-loss",
        gross_amount=10,
        idempotency_key="before-authority-loss",
        occurred_at=now,
        resolved_config=resolved,
    )
    assert created and settlement.gross_amount == 10
    with pytest.raises(ValidationError):
        settle_clan_reward(
            membership=member,
            source_type="CLAN_ACTIVITY",
            source_id="after-authority-loss",
            gross_amount=10,
            idempotency_key="after-authority-loss",
            occurred_at=timezone.now(),
            resolved_config=resolved,
        )
    with pytest.raises(ValidationError):
        purchase_item(
            clan_season=cs,
            actor=cs.captain,
            item_id="recruitment_expansion",
            idempotency_key="blocked-purchase",
            resolved_config=resolved,
        )
    with pytest.raises(ClanConflict):
        request_join(make_player(ClanRole.SOLDIER), cs, resolved)


def test_verified_membership_demotion_and_stale_update_regression(authority_world):
    cs, _, _ = authority_world
    now = timezone.now().replace(microsecond=0)
    for update_id, status in [(100, "member"), (99, "administrator")]:
        row = TelegramInboundUpdate.objects.create(
            update_id=update_id,
            payload={
                "update_id": update_id,
                "chat_member": {
                    "date": int(now.timestamp()),
                    "chat": {
                        "id": cs.clan.telegram_group.telegram_chat_id,
                        "type": "supergroup",
                        "title": "Isolated Clan",
                    },
                    "from": {"id": 999},
                    "old_chat_member": {
                        "status": "administrator",
                        "user": {"id": cs.captain.telegram_user_id},
                    },
                    "new_chat_member": {
                        "status": status,
                        "user": {"id": cs.captain.telegram_user_id},
                    },
                },
            },
            payload_expires_at=now + timedelta(days=1),
        )
        process_inbound_update(row.pk)
    cs.refresh_from_db()
    assert cs.state == "RECOVERY" and cs.recovery_reason == "CAPTAIN_AUTHORITY_LOSS"


def test_concurrent_authority_transitions_keep_newest_evidence(authority_world):
    cs, _, _ = authority_world
    now = timezone.now()
    barrier = Barrier(2)

    def run(offset):
        close_old_connections()
        try:
            barrier.wait()
            return observe_captain_authority(
                cs.pk,
                has_authority=offset == 1,
                authoritative=True,
                observed_at=now + timedelta(seconds=offset),
                fresh_verification=True,
                bot_authorized=True,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        [f.result(timeout=20) for f in [pool.submit(run, n) for n in (0, 1)]]
    cs.refresh_from_db()
    assert cs.state == "ACTIVE" and cs.captain_authority_lost_at is None


def test_bounded_durable_recheck_retries_unknown_state(authority_world, settings):
    cs, state, calls = authority_world
    verify_captain_authority(cs.pk)
    state["unknown"] = True
    assert recheck_lost_captain_authority(limit=1) == 1
    assert recheck_lost_captain_authority(limit=1) == 0
    cs.refresh_from_db()
    assert cs.state == "RECOVERY"
    ClanSeason.objects.filter(pk=cs.pk).update(
        captain_authority_last_attempt_at=timezone.now()
        - timedelta(seconds=settings.CLAN_AUTHORITY_RECHECK_SECONDS + 1)
    )
    state.update(unknown=False, captain="administrator")
    assert recheck_lost_captain_authority(limit=1) == 1
    cs.refresh_from_db()
    assert cs.state == "ACTIVE"


def test_recovery_verify_api_is_captain_only(authority_world, clan_world, init_data_factory):
    cs, state, _ = authority_world
    client = Client()
    path = "/api/v1/clans/recovery/verify"
    assert client.post(path).status_code == 401
    client.defaults["HTTP_AUTHORIZATION"] = "tma " + init_data_factory(
        user={"id": cs.captain.telegram_user_id, "first_name": "Captain"}
    )
    response = client.post(path)
    assert response.status_code == 200
    assert response.json()["reasons"] == ["CAPTAIN_AUTHORITY_LOSS"]
    assert response.json()["can_verify_authority"] is True
    captured = {"authority_lost": response.json()}
    state["captain"] = "creator"
    restored = client.post(path)
    assert restored.status_code == 200 and restored.json()["state"] == "ACTIVE"
    captured["authority_restored"] = restored.json()
    if os.getenv("B5_EXPORT_FIXTURES") == "1":
        Path("docs/fixtures/b5/authority-http.json").write_text(
            json.dumps(captured, indent=2) + "\n"
        )

    now, config, season, _, make_player, _ = clan_world
    soldier = make_player(ClanRole.SOLDIER)
    ClanMembership.objects.create(
        player=soldier,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now,
        config_version=config,
    )
    client.defaults["HTTP_AUTHORIZATION"] = "tma " + init_data_factory(
        user={"id": soldier.telegram_user_id, "first_name": "Soldier"}
    )
    assert client.post(path).status_code == 403


def test_inflight_verification_cannot_clear_a_newer_demotion(
    authority_world, monkeypatch, settings
):
    cs, state, _ = authority_world
    verify_captain_authority(cs.pk)

    def stale_success(chat_id, user_id):
        if user_id == settings.TELEGRAM_BOT_ID:
            observe_captain_authority(
                cs.pk, has_authority=False, authoritative=True, observed_at=timezone.now()
            )
        return TelegramResult(True, result={"status": "administrator", "can_invite_users": True})

    monkeypatch.setattr("apps.telegram.bot_api.get_chat_member", stale_success)
    assert verify_captain_authority(cs.pk).state == "RECOVERY"


def test_authority_restore_never_cancels_opened_governance(authority_world):
    cs, state, _ = authority_world
    verify_captain_authority(cs.pk)
    # Existing governance episodes are retained even after every live cause recovers.
    ClanSeason.objects.filter(pk=cs.pk).update(recovery_governance_opened_at=timezone.now())
    state["captain"] = "creator"
    cs = verify_captain_authority(cs.pk)
    assert cs.state == "RECOVERY" and cs.captain_authority_lost_at is None
