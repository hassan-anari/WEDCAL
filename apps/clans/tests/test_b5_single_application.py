# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from django.db import close_old_connections
from django.test import Client
from django.utils import timezone

from apps.clans.models import ClanInvite, ClanJoinRequest, ClanMembership, ClanRole, ClanSeason
from apps.clans.services import (
    ClanConflict,
    cancel_request,
    create_personal_invite,
    decide_request,
    reconcile_telegram_membership,
    recover_invite_creations,
    request_join,
    revoke_invite_remote,
)
from apps.seasons.models import Season
from apps.telegram.bot_api import TelegramResult

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def synthetic_only(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_kw: pytest.fail("No live Telegram")
    )
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)


def parallel(*calls):
    barrier = Barrier(len(calls))

    def run(call):
        close_old_connections()
        try:
            barrier.wait()
            return call()
        except ClanConflict as exc:
            return exc
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(run, call) for call in calls]
        return [future.result(timeout=20) for future in futures]


@pytest.mark.parametrize("accepted", [False, True])
def test_one_active_application_across_clans_and_idempotent_replay(clan_world, accepted):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = request_join(soldier, first, resolved)
    if accepted:
        decide_request(first.captain, original.pk, accept=True)
    assert request_join(soldier, first, resolved).pk == original.pk
    with pytest.raises(ClanConflict):
        request_join(soldier, second, resolved)
    assert ClanJoinRequest.objects.filter(applicant=soldier).count() == 1


def test_parallel_applications_to_different_clans_have_exactly_one_winner(clan_world):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    results = parallel(
        lambda: request_join(soldier, first, resolved),
        lambda: request_join(soldier, second, resolved),
    )
    assert sum(isinstance(row, ClanJoinRequest) for row in results) == 1
    assert sum(isinstance(row, ClanConflict) for row in results) == 1
    assert ClanJoinRequest.objects.filter(applicant=soldier, state="PENDING").count() == 1


def test_parallel_duplicates_return_the_same_request(clan_world):
    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    results = parallel(*[lambda: request_join(soldier, cs, resolved) for _ in range(2)])
    assert all(isinstance(row, ClanJoinRequest) for row in results)
    assert len({row.pk for row in results}) == 1


@pytest.mark.parametrize("terminal", ["CANCELLED", "DECLINED", "SAFE_ACCEPTED_CANCEL"])
def test_explicit_safe_resolution_allows_next_application_without_deleting_history(
    clan_world, terminal
):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = request_join(soldier, first, resolved)
    created_at = original.created_at
    if terminal == "DECLINED":
        decide_request(first.captain, original.pk, accept=False)
    else:
        if terminal == "SAFE_ACCEPTED_CANCEL":
            decide_request(first.captain, original.pk, accept=True)
        cancel_request(soldier, original.pk)
    next_request = request_join(soldier, second, resolved)
    original.refresh_from_db()
    assert original.state in {"CANCELLED", "DECLINED"}
    assert original.created_at == created_at and next_request.pk != original.pk
    assert ClanJoinRequest.objects.filter(applicant=soldier).count() == 2


@pytest.mark.parametrize(
    "state", ["CREATING", "ACTIVE", "UNCERTAIN", "REVOKE_PENDING", "REVOKE_FAILED", "MANUAL_REVIEW"]
)
def test_unresolved_accepted_credential_keeps_global_application_and_reservation(clan_world, state):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = decide_request(first.captain, request_join(soldier, first, resolved).pk, accept=True)
    ClanInvite.objects.filter(join_request=original).update(
        state=state, unknown_credential_risk=True
    )
    cancel_request(soldier, original.pk)
    with pytest.raises(ClanConflict):
        request_join(soldier, second, resolved)
    with pytest.raises(ClanConflict):
        request_join(soldier, first, resolved)
    original.refresh_from_db()
    assert original.state == "ACCEPTED"
    assert first.join_requests.filter(state="ACCEPTED").count() == 1


@pytest.mark.parametrize("history", ["CANCELLED", "DECLINED", "ACTIVATED"])
@pytest.mark.parametrize(
    "unsafe", ["CREATING", "UNCERTAIN", "REVOKE_PENDING", "REVOKE_FAILED", "MANUAL_REVIEW"]
)
def test_terminal_legacy_label_cannot_hide_unsafe_credential_in_another_home(
    clan_world, history, unsafe
):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = ClanJoinRequest.objects.create(applicant=soldier, clan_season=first, state=history)
    ClanInvite.objects.create(join_request=original, state=unsafe, unknown_credential_risk=False)
    with pytest.raises(ClanConflict):
        request_join(soldier, second, resolved)
    original.refresh_from_db()
    assert original.state == history


def test_acceptance_racing_another_clan_application_cannot_replace_the_original(clan_world):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = request_join(soldier, first, resolved)
    results = parallel(
        lambda: decide_request(first.captain, original.pk, accept=True),
        lambda: request_join(soldier, second, resolved),
    )
    assert isinstance(results[0], ClanJoinRequest) and results[0].state == "ACCEPTED"
    assert isinstance(results[1], ClanConflict)
    assert ClanJoinRequest.objects.filter(applicant=soldier).count() == 1


def test_legacy_overlap_requires_explicit_resolution_and_preserves_accepted_path(
    clan_world, monkeypatch
):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    accepted = decide_request(first.captain, request_join(soldier, first, resolved).pk, accept=True)
    legacy = ClanJoinRequest.objects.create(applicant=soldier, clan_season=second)
    calls = []

    def create(*_args):
        calls.append(1)
        return TelegramResult(True, {"invite_link": "synthetic-credential"})

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", create)
    with pytest.raises(ClanConflict):
        decide_request(second.captain, legacy.pk, accept=True)
    with pytest.raises(ClanConflict):
        create_personal_invite(accepted.invite)
    assert not calls
    assert decide_request(first.captain, accepted.pk, accept=True).pk == accepted.pk
    cancel_request(soldier, legacy.pk)
    assert create_personal_invite(accepted.invite).state == "ACTIVE"
    accepted.refresh_from_db()
    assert accepted.state == "ACCEPTED" and len(calls) == 1


def test_old_season_active_path_blocks_new_season_but_safe_history_does_not(clan_world):
    now, config, _, resolved, player, clan = clan_world
    soldier, current = player(ClanRole.SOLDIER), clan()
    old_season = Season.objects.create(
        code="legacy-application-season",
        title="Synthetic old Season",
        starts_at=now - timedelta(days=100),
        ends_at=now - timedelta(days=50),
        baseline_config=config,
    )
    old_cs = ClanSeason.objects.create(
        clan=current.clan,
        season=old_season,
        captain=current.captain,
        base_capacity=20,
        config_version=config,
    )
    original = ClanJoinRequest.objects.create(applicant=soldier, clan_season=old_cs)
    with pytest.raises(ClanConflict):
        request_join(soldier, current, resolved)
    cancel_request(soldier, original.pk)
    assert request_join(soldier, current, resolved).clan_season_id == current.pk
    original.refresh_from_db()
    assert original.clan_season.season_id == old_season.pk and original.state == "CANCELLED"


@pytest.mark.parametrize("state", ["CREATING", "UNCERTAIN"])
def test_inactive_recovery_cannot_fake_safe_revocation_of_uncertain_credential(clan_world, state):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = decide_request(first.captain, request_join(soldier, first, resolved).pk, accept=True)
    # Explicit legacy label inconsistency; the credential remains an obligation.
    ClanJoinRequest.objects.filter(pk=original.pk).update(state="CANCELLED")
    ClanInvite.objects.filter(join_request=original).update(
        state=state,
        unknown_credential_risk=False,
        claim_expires_at=timezone.now() - timedelta(seconds=1),
        uncertain_since=timezone.now() - timedelta(days=1),
    )
    assert recover_invite_creations() == 1
    original.invite.refresh_from_db()
    assert original.invite.state == "MANUAL_REVIEW" and original.invite.unknown_credential_risk
    with pytest.raises(ClanConflict):
        request_join(soldier, second, resolved)


@pytest.mark.parametrize("revoke_ok", [False, True])
def test_membership_activation_racing_cancel_and_remote_revoke_preserves_obligations(
    clan_world, monkeypatch, revoke_ok
):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = decide_request(first.captain, request_join(soldier, first, resolved).pk, accept=True)
    ClanInvite.objects.filter(join_request=original).update(
        state="ACTIVE", invite_link="synthetic-credential"
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.revoke_chat_invite_link",
        lambda *_: TelegramResult(
            revoke_ok, {}, category=None if revoke_ok else "synthetic_failure"
        ),
    )

    def cancel_and_revoke():
        cancel_request(soldier, original.pk)
        return revoke_invite_remote(original.invite.pk)

    results = parallel(
        cancel_and_revoke,
        lambda: reconcile_telegram_membership(
            player=soldier,
            group=first.clan.telegram_group,
            present=True,
            telegram_update_id=100,
            observed_at=timezone.now(),
        ),
    )
    assert not any(isinstance(row, Exception) for row in results)
    original.refresh_from_db()
    members = ClanMembership.objects.filter(player=soldier, ended_at__isnull=True).count()
    reservations = first.join_requests.filter(applicant=soldier, state="ACCEPTED").count()
    assert members + reservations <= 1
    if members or reservations:
        with pytest.raises(ClanConflict):
            request_join(soldier, second, resolved)
    else:
        assert revoke_ok and original.state == "CANCELLED"
        assert request_join(soldier, second, resolved).clan_season_id == second.pk
    assert ClanJoinRequest.objects.filter(pk=original.pk).exists()


def test_http_conflict_is_typed_private_and_ph048_is_deferred(
    clan_world, init_data_factory, settings
):
    _, _, _, _, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    client = Client(
        HTTP_AUTHORIZATION="tma "
        + init_data_factory(user={"id": soldier.telegram_user_id, "first_name": "Soldier"})
    )
    first_response = client.post(f"/api/v1/clans/{first.clan_id}/join-requests")
    conflict = client.post(f"/api/v1/clans/{second.clan_id}/join-requests")
    assert first_response.status_code == 200 and conflict.status_code == 409
    assert conflict.json()["code"] == "CLAN_STATE_CONFLICT"
    assert "request" not in conflict.json() and "clan_id" not in conflict.json()
    assert conflict["Cache-Control"] == "private, no-store"
    assert client.get("/api/v1/clans/state").json()["owner_decisions_pending"] == []
    if os.getenv("B5_EXPORT_FIXTURES") == "1":
        Path("docs/fixtures/b5/single-application-http.json").write_text(
            json.dumps(
                {"first_request": first_response.json(), "second_clan_conflict": conflict.json()},
                indent=2,
            )
            + "\n"
        )


@pytest.mark.parametrize("state", ["PENDING", "FAILED"])
def test_obsolete_unissued_join_cannot_create_a_credential_after_new_application(clan_world, state):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = ClanJoinRequest.objects.create(
        applicant=soldier, clan_season=first, state="CANCELLED"
    )
    invite = ClanInvite.objects.create(join_request=original, state=state)
    new = request_join(soldier, second, resolved)
    with pytest.raises(ClanConflict):
        create_personal_invite(invite)
    invite.refresh_from_db()
    assert invite.attempt_count == 0 and new.state == "PENDING"


def test_existing_accepted_activation_preserves_legacy_pending_audit(clan_world):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    accepted = decide_request(first.captain, request_join(soldier, first, resolved).pk, accept=True)
    legacy = ClanJoinRequest.objects.create(applicant=soldier, clan_season=second)
    reconcile_telegram_membership(
        player=soldier,
        group=first.clan.telegram_group,
        present=True,
        telegram_update_id=100,
        observed_at=timezone.now(),
    )
    accepted.refresh_from_db()
    legacy.refresh_from_db()
    assert accepted.state == "ACTIVATED" and legacy.state == "PENDING"
    assert (
        ClanMembership.objects.filter(
            player=soldier, clan_season=first, ended_at__isnull=True
        ).count()
        == 1
    )
    with pytest.raises(ClanConflict):
        decide_request(second.captain, legacy.pk, accept=True)


@pytest.mark.parametrize("unsafe", [False, True])
def test_ended_rejoin_cannot_bypass_new_application_or_issue_more_credentials(clan_world, unsafe):
    now, config, season, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    old_member = ClanMembership.objects.create(
        player=soldier,
        season=season,
        clan_season=first,
        role=ClanRole.SOLDIER,
        started_at=now - timedelta(days=2),
        ended_at=now - timedelta(days=1),
        access_state="ACCESS_LOST",
        config_version=config,
    )
    invite = ClanInvite.objects.create(
        membership=old_member,
        purpose="REJOIN",
        state="UNCERTAIN" if unsafe else "PENDING",
        unknown_credential_risk=unsafe,
    )
    if unsafe:
        with pytest.raises(ClanConflict):
            request_join(soldier, second, resolved)
    else:
        assert request_join(soldier, second, resolved).state == "PENDING"
        with pytest.raises(ClanConflict):
            create_personal_invite(invite)
        invite.refresh_from_db()
        assert invite.attempt_count == 0


@pytest.mark.parametrize("already_terminal", [False, True])
def test_confirmed_manual_revocation_safely_releases_accepted_request(clan_world, already_terminal):
    from django.contrib.auth.models import User

    from apps.clans.services import resolve_invite_credential_risk

    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = decide_request(first.captain, request_join(soldier, first, resolved).pk, accept=True)
    if already_terminal:
        ClanInvite.objects.filter(join_request=original).update(state="REVOKED")
        cancel_request(soldier, original.pk)
    else:
        ClanInvite.objects.filter(join_request=original).update(
            state="MANUAL_REVIEW", unknown_credential_risk=True
        )
        actor = User.objects.create_superuser(username="synthetic-proof-reviewer")
        resolve_invite_credential_risk(
            invite_id=original.invite.pk,
            outcome="CONFIRMED_REVOKED",
            actor=actor,
            reason="Synthetic positive revocation proof",
        )
    original.refresh_from_db()
    assert original.state == "CANCELLED"
    assert request_join(soldier, second, resolved).state == "PENDING"


def test_terminal_legacy_credential_risk_remains_in_manual_review_until_proven(clan_world):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = decide_request(first.captain, request_join(soldier, first, resolved).pk, accept=True)
    ClanInvite.objects.filter(join_request=original).update(
        state="REVOKED", unknown_credential_risk=True
    )
    cancel_request(soldier, original.pk)
    original.refresh_from_db()
    original.invite.refresh_from_db()
    assert original.state == "ACCEPTED" and original.invite.state == "MANUAL_REVIEW"
    with pytest.raises(ClanConflict):
        request_join(soldier, second, resolved)


def test_manual_proof_racing_captain_cancel_uses_applicant_first_lock_order(clan_world):
    from django.contrib.auth.models import User

    from apps.clans.services import captain_cancel_accepted_request, resolve_invite_credential_risk

    _, _, _, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    original = decide_request(cs.captain, request_join(soldier, cs, resolved).pk, accept=True)
    ClanInvite.objects.filter(join_request=original).update(
        state="MANUAL_REVIEW", unknown_credential_risk=True
    )
    actor = User.objects.create_superuser(username="concurrent-proof-reviewer")
    results = parallel(
        lambda: captain_cancel_accepted_request(cs.captain, original.pk),
        lambda: resolve_invite_credential_risk(
            invite_id=original.invite.pk,
            outcome="CONFIRMED_REVOKED",
            actor=actor,
            reason="Synthetic positive proof",
        ),
    )
    assert all(not isinstance(row, Exception) or isinstance(row, ClanConflict) for row in results)
    original.refresh_from_db()
    original.invite.refresh_from_db()
    assert original.state == "CANCELLED" and not original.invite.unknown_credential_risk


def test_cancel_during_unambiguous_create_failure_safely_terminalizes_parent(
    clan_world, monkeypatch
):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = decide_request(first.captain, request_join(soldier, first, resolved).pk, accept=True)

    def remote_failure(*_args):
        cancel_request(soldier, original.pk)
        return TelegramResult(False, category="forbidden")

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", remote_failure)
    assert create_personal_invite(original.invite).state == "REVOKED"
    original.refresh_from_db()
    assert original.state == "CANCELLED"
    assert request_join(soldier, second, resolved).state == "PENDING"


@pytest.mark.parametrize("state", ["ACTIVE", "REVOKE_PENDING", "REVOKE_FAILED", "MANUAL_REVIEW"])
def test_missing_legacy_credential_metadata_is_not_safe_release_proof(clan_world, state):
    _, _, _, resolved, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    original = decide_request(first.captain, request_join(soldier, first, resolved).pk, accept=True)
    ClanInvite.objects.filter(join_request=original).update(
        state=state, unknown_credential_risk=False, invite_link=""
    )
    cancel_request(soldier, original.pk)
    original.refresh_from_db()
    original.invite.refresh_from_db()
    assert original.state == "ACCEPTED" and original.invite.state == "MANUAL_REVIEW"
    assert original.invite.unknown_credential_risk
    with pytest.raises(ClanConflict):
        request_join(soldier, second, resolved)
