# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.test import Client
from django.utils import timezone

from apps.clans.economy import settle_clan_reward
from apps.clans.missions import process_mission_signals, queue_mission_signal
from apps.clans.models import (
    ClanInvite,
    ClanMembership,
    ClanMembershipEvent,
    ClanMissionDefinition,
    ClanMissionSignal,
    ClanRole,
)
from apps.clans.services import decide_request, request_join, terminalize_closed_clan_credentials

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def b5_http(clan_world, init_data_factory, settings, monkeypatch):
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_kw: pytest.fail("No live Telegram")
    )

    def client_for(person):
        client = Client()
        client.defaults["HTTP_AUTHORIZATION"] = "tma " + init_data_factory(
            user={"id": person.telegram_user_id, "first_name": person.first_name}
        )
        return client

    return client_for


def test_actual_clan_reads_privacy_economy_and_generated_fixtures(clan_world, b5_http):
    now, config, season, resolved, player, clan = clan_world
    cs = clan()
    soldier = player(ClanRole.SOLDIER)
    member = ClanMembership.objects.create(
        player=soldier,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now,
        config_version=config,
    )
    ClanMembershipEvent.objects.create(
        membership=member, kind="ACTIVATED", occurred_at=member.started_at
    )
    outsider = player(ClanRole.SOLDIER)
    captain_http, soldier_http, outsider_http = (
        b5_http(cs.captain),
        b5_http(soldier),
        b5_http(outsider),
    )
    settlement, _ = settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="b5-isolated-source",
        gross_amount=100000,
        idempotency_key="b5-isolated-source",
        occurred_at=timezone.now(),
        resolved_config=resolved,
    )
    captured = {}
    for path in [
        "state",
        "home",
        "roster",
        "contributions",
        "treasury",
        "equipment",
        "shop",
        "inventory",
        "missions",
        "trophies",
        "governance",
        "recovery",
        "membership/current",
        "join-requests/current",
    ]:
        response = captain_http.get("/api/v1/clans/" + path)
        assert response.status_code == 200, (path, response.content)
        assert response["Cache-Control"] == "private, no-store"
        captured[path] = response.json()
    assert captured["roster"]["total_count"] == 2
    assert {m["membership_id"] for m in captured["roster"]["items"]} == {
        member.pk,
        cs.memberships.get(role=ClanRole.CAPTAIN).pk,
    }
    assert all("telegram_user_id" not in m for m in captured["roster"]["items"])
    assert captured["home"]["total_contribution"] == settlement.contribution_amount
    assert captured["trophies"][0]["blocked_reason"] == "PH-048_PRODUCT_DECISION_REQUIRED"
    assert captured["trophies"][0]["unlocked"] is False
    assert outsider_http.get("/api/v1/clans/roster").status_code == 404
    assert outsider_http.get("/api/v1/clans/treasury").status_code == 404
    assert all(
        not item["can_purchase"]
        for item in soldier_http.get("/api/v1/clans/equipment").json()["items"]
    )
    payload = {"idempotency_key": "b5-http-purchase"}
    path = "/api/v1/clans/shop/rally-beacon/purchase"
    first = captain_http.post(path, data=payload, content_type="application/json")
    assert first.status_code == 200, first.content
    retry = captain_http.post(path, data=payload, content_type="application/json")
    assert retry.json()["replayed"] is True
    assert (
        soldier_http.post(
            path, data={"idempotency_key": "b5-wrong-role"}, content_type="application/json"
        ).status_code
        == 403
    )
    activate = captain_http.post(
        "/api/v1/clans/inventory/rally-beacon/activate",
        data={"idempotency_key": "b5-http-use"},
        content_type="application/json",
    )
    assert activate.status_code == 200, activate.content
    captured["equipment_after_activation"] = captain_http.get("/api/v1/clans/equipment").json()
    assert captured["equipment_after_activation"]["active_effects"][0]["effective"] is True
    assert captured["equipment_after_activation"]["items"][0]["quantity"] == 0
    captured["purchase"] = first.json()
    captured["purchase_replay"] = retry.json()
    captured["forbidden"] = soldier_http.post(
        path, data={"idempotency_key": "b5-wrong-role-2"}, content_type="application/json"
    ).json()
    if os.getenv("B5_EXPORT_FIXTURES"):
        Path("docs/fixtures/b5").mkdir(parents=True, exist_ok=True)
        Path("docs/fixtures/b5/clan-http.json").write_text(
            json.dumps(captured, indent=2, default=str) + "\n"
        )


def test_own_requests_and_invite_credentials_do_not_leak(clan_world, b5_http):
    _, _, _, resolved, player, clan = clan_world
    cs, second = clan(), clan()
    soldier, other = player(ClanRole.SOLDIER), player(ClanRole.SOLDIER)
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    ClanInvite.objects.filter(join_request=req).update(
        state="ACTIVE", invite_link="https://t.me/+PRIVATE_SYNTHETIC_CREDENTIAL"
    )
    own = b5_http(soldier).get("/api/v1/clans/join-requests/current")
    assert own.json()[0]["id"] == req.pk and own.json()[0]["invite_state"] == "ACTIVE"
    assert "PRIVATE_SYNTHETIC_CREDENTIAL" not in own.content.decode()
    assert b5_http(other).get("/api/v1/clans/join-requests/current").json() == []
    assert b5_http(other).get("/api/v1/clans/invite/current").status_code == 404
    assert b5_http(second.captain).get("/api/v1/clans/applicants").json() == []
    assert (
        b5_http(second.captain).post(f"/api/v1/clans/join-requests/{req.pk}/revoke").status_code
        == 403
    )


def test_closed_clan_pending_requests_cancel_but_unknown_credentials_stay_reserved(
    clan_world, monkeypatch
):
    _, _, _, resolved, player, clan = clan_world
    cs = clan()
    soldier = player(ClanRole.SOLDIER)
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    ClanInvite.objects.filter(join_request=req).update(
        state="UNCERTAIN", unknown_credential_risk=True
    )
    cs.state = "ENDED"
    cs.save()
    terminalize_closed_clan_credentials()
    req.refresh_from_db()
    assert req.state == "ACCEPTED"
    req.invite.refresh_from_db()
    assert req.invite.unknown_credential_risk and req.invite.desired_terminal_state == "REVOKED"


@pytest.mark.parametrize("kind", ["INDIVIDUAL", "GROUP"])
def test_real_durable_mission_signal_progress_settlement_and_replay(clan_world, kind):
    now, config, season, _, player, clan = clan_world
    cs = clan()
    soldier = player(ClanRole.SOLDIER)
    member = ClanMembership.objects.create(
        player=soldier,
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now - timedelta(seconds=1),
        config_version=config,
    )
    ClanMembershipEvent.objects.create(
        membership=member, kind="ACTIVATED", occurred_at=member.started_at
    )
    definition = ClanMissionDefinition.objects.create(
        key="b5-isolated-contribution",
        version=1,
        kind=kind,
        requirement="VALID_TYPING_RUN",
        target_units=1,
        reward_band="GROUP" if kind == "GROUP" else "SMALL",
        starts_at=now - timedelta(minutes=1),
        ends_at=now + timedelta(hours=1),
        enabled=True,
        config_version=config,
    )
    queue_mission_signal(
        membership=member,
        source_key="b5-typing-source",
        requirement="VALID_TYPING_RUN",
        units=1,
        occurred_at=now,
    )
    queue_mission_signal(
        membership=member,
        source_key="b5-typing-source",
        requirement="VALID_TYPING_RUN",
        units=1,
        occurred_at=now,
    )
    assert ClanMissionSignal.objects.count() == 1
    assert process_mission_signals() == 1
    assert process_mission_signals() == 0
    progress = definition.clanmissionprogress_set.get()
    assert progress.completed_at == now
    assert progress.participants.get(membership=member).reward_settlement is not None
    assert cs.economic_settlements.filter(source_type="CLAN_MISSION").count() == 1


def test_actual_typing_submission_queues_durable_mission_and_blocks_close_drain(clan_world):
    from apps.seasons.close import _clan_mission_signals
    from apps.typing_game.models import TypingPhrase
    from apps.typing_game.services import start_attempt, submit_attempt

    now, config, season, _, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    phrase = TypingPhrase.objects.create(
        key="b5-typing", version=1, text="steady clans build durable shared progress", enabled=True
    )
    attempt, _ = start_attempt(cs.captain, "b5-signal-attempt", now + timedelta(seconds=1))
    result, created = submit_attempt(
        cs.captain, attempt.public_id, phrase.text, now + timedelta(seconds=12)
    )
    assert created and result.status == "VALID"
    assert (
        ClanMissionSignal.objects.filter(requirement="VALID_TYPING_RUN", membership=member).count()
        == 1
    )
    submit_attempt(cs.captain, attempt.public_id, phrase.text, now + timedelta(seconds=12))
    assert ClanMissionSignal.objects.count() == 1
    evidence = _clan_mission_signals(season, now + timedelta(hours=1))
    assert evidence.pending_count == 1
    assert process_mission_signals() == 1
    assert _clan_mission_signals(season, now + timedelta(hours=1)).pending_count == 0


def test_actual_governance_lifecycle_and_credential_failure_fixtures(
    clan_world, b5_http, monkeypatch
):
    from apps.clans.governance import finalize_case
    from apps.clans.services import reconcile_telegram_membership
    from apps.telegram.bot_api import TelegramResult

    now, config, season, resolved, player, clan = clan_world
    cs = clan()
    soldiers = [player(ClanRole.SOLDIER) for _ in range(3)]
    memberships = [
        ClanMembership.objects.create(
            player=p,
            season=season,
            clan_season=cs,
            role=ClanRole.SOLDIER,
            started_at=now,
            config_version=config,
        )
        for p in soldiers
    ]
    for member in memberships:
        ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    captain = b5_http(cs.captain)
    target = memberships[0]
    response = captain.post(
        "/api/v1/clans/removal-cases",
        data={"target_membership_id": target.pk, "reason": "COMMUNITY_REASON"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    case = response.json()
    captured = {"governance_open": case}
    assert case["eligible_count"] == 3 and case["yes_count"] is None
    denied = b5_http(target.player).post(
        f"/api/v1/clans/governance/{case['id']}/vote",
        data={"choice": "YES"},
        content_type="application/json",
    )
    assert denied.status_code == 403
    captured["target_vote_forbidden"] = denied.json()
    for voter, choice in [(cs.captain, "YES"), (soldiers[1], "YES"), (soldiers[2], "NO")]:
        assert (
            b5_http(voter)
            .post(
                f"/api/v1/clans/governance/{case['id']}/vote",
                data={"choice": choice},
                content_type="application/json",
            )
            .status_code
            == 200
        )
    finalize_case(case["id"], now=now + timedelta(hours=25))
    final = captain.get("/api/v1/clans/governance").json()[0]
    assert final["status"] == "APPROVED" and final["yes_count"] == 2
    captured["governance_final"] = final

    # Real existing Telegram reconciliation, including loss and recovery, never
    # equates group absence to membership deletion.
    returning = memberships[1]
    reconcile_telegram_membership(
        player=returning.player,
        group=cs.clan.telegram_group,
        present=False,
        telegram_update_id=100,
        observed_at=now + timedelta(seconds=1),
    )
    captured["access_lost"] = (
        b5_http(returning.player).get("/api/v1/clans/membership/current").json()
    )
    assert captured["access_lost"]["access_state"] == "ACCESS_LOST"
    reconcile_telegram_membership(
        player=returning.player,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=101,
        observed_at=now + timedelta(seconds=2),
    )
    captured["access_recovered"] = (
        b5_http(returning.player).get("/api/v1/clans/membership/current").json()
    )
    assert captured["access_recovered"]["access_state"] == "ACTIVE"

    applicant = player(ClanRole.SOLDIER)
    applicant_http = b5_http(applicant)
    req = applicant_http.post(f"/api/v1/clans/{cs.clan_id}/join-requests").json()
    captured["pending_request"] = applicant_http.get("/api/v1/clans/join-requests/current").json()
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"status": "left"}),
    )
    monkeypatch.setattr(
        "apps.clans.services.bot_api.create_chat_invite_link",
        lambda *_: TelegramResult(False, category="timeout", ambiguous=True),
    )
    accepted = captain.post(
        f"/api/v1/clans/join-requests/{req['id']}/decision",
        data={"accept": True},
        content_type="application/json",
    )
    assert accepted.status_code == 200
    captured["accepted_request"] = accepted.json()
    captured["uncertain_invite"] = applicant_http.get("/api/v1/clans/invite/current").json()
    assert (
        captured["uncertain_invite"]["state"] == "UNCERTAIN"
        and captured["uncertain_invite"]["invite_link"] is None
    )
    applicant_http.delete(f"/api/v1/clans/join-requests/{req['id']}")
    captured["unsafe_cancellation_retained"] = applicant_http.get(
        "/api/v1/clans/join-requests/current"
    ).json()
    assert captured["unsafe_cancellation_retained"][0]["state"] == "ACCEPTED"
    assert captured["unsafe_cancellation_retained"][0]["cancellation_pending"]
    if os.getenv("B5_EXPORT_FIXTURES"):
        Path("docs/fixtures/b5/lifecycle-http.json").write_text(
            json.dumps(captured, indent=2, default=str) + "\n"
        )


def test_owner_approved_same_season_recreation_is_blocked(clan_world, b5_http):
    from apps.clans.governance import request_dissolution

    _, _, _, _, _, clan = clan_world
    cs = clan()
    request_dissolution(cs.captain, cs)
    response = b5_http(cs.captain).post(
        "/api/v1/clans/",
        data={"telegram_group_id": 999, "name": "No recreation", "tag": "NORE"},
        content_type="application/json",
    )
    assert response.status_code == 409 and response.json()["code"] == "CLAN_RECREATION_BLOCKED"
