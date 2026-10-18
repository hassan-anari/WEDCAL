# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.test import Client

from apps.clans.governance import GovernanceConflict, open_case, request_dissolution
from apps.clans.models import ClanGovernanceCase, ClanRole, ClanSeason
from apps.clans.tests.test_phase7b_governance import add_member
from apps.players.models import Player

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def governance_http(init_data_factory, settings):
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"

    def make(person):
        return Client(
            HTTP_AUTHORIZATION="tma "
            + init_data_factory(user={"id": person.telegram_user_id, "first_name": "QA"})
        )

    return make


@pytest.mark.parametrize("members", [2, 3, 4])
def test_thresholds_target_exclusion_and_http_preflight(clan_world, governance_http, members):
    cs = clan_world[-1]()
    soldiers = [add_member(clan_world, cs) for _ in range(members - 1)]
    http = governance_http(cs.captain)
    state = http.get("/api/v1/clans/recovery").json()["governance"]
    assert state["config_version"] == clan_world[1].version
    assert state["config_checksum"] == clan_world[1].payload_checksum
    assert state["minimum_eligible_voters"] == 3
    assert state["dissolution"]["eligible_count"] == members
    assert not state["dissolution"]["direct"]
    assert state["dissolution"]["can_open"] is (members >= 3)
    assert state["dissolution"]["outcome"] == ("VOTE_REQUIRED" if members >= 3 else "BLOCKED")
    assert state["dissolution"]["same_season_recreation_allowed"] is False
    assert "same Season" in state["dissolution"]["confirmation_warning"]
    target = next(
        x for x in state["removal_targets"] if x["target_membership_id"] == soldiers[0].pk
    )
    assert target["eligible_count"] == members - 1
    assert target["can_open"] is (members >= 4)
    dissolution = http.post("/api/v1/clans/dissolution")
    removal = http.post(
        "/api/v1/clans/removal-cases",
        data={"target_membership_id": soldiers[0].pk, "reason": "SPAM"},
        content_type="application/json",
    )
    for response, allowed in [(dissolution, members >= 3), (removal, members >= 4)]:
        assert response.status_code == (200 if allowed else 409), response.content
        if not allowed:
            assert response.json()["code"] == "CLAN_GOVERNANCE_INSUFFICIENT_VOTERS"
            assert response.json()["retryable"] is False
    if members >= 4:
        case = ClanGovernanceCase.objects.get(pk=removal.json()["id"])
        assert not case.electors.filter(player=soldiers[0].player).exists()
        assert case.eligible_count == members - 1


def test_invalid_targets_open_case_replay_and_isolation(clan_world, governance_http):
    cs = clan_world[-1]()
    target, second, _ = [add_member(clan_world, cs) for _ in range(3)]
    outsider = clan_world[-1]()
    foreign = add_member(clan_world, outsider)
    http = governance_http(cs.captain)
    path = "/api/v1/clans/removal-cases"

    def post(target_id, reason="SPAM"):
        return http.post(
            path,
            data={"target_membership_id": target_id, "reason": reason},
            content_type="application/json",
        )

    for target_id in [0, foreign.pk, cs.memberships.get(role=ClanRole.CAPTAIN).pk]:
        assert post(target_id).json()["code"] == "CLAN_GOVERNANCE_INVALID_TARGET"
    assert post(target.pk, "unknown").json()["code"] == "CLAN_GOVERNANCE_INVALID_REMOVAL_REASON"
    first = post(target.pk)
    assert first.status_code == 200
    assert post(target.pk).json()["id"] == first.json()["id"]
    conflict = post(second.pk)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CLAN_GOVERNANCE_OPEN_CASE_EXISTS"
    preflight = http.get("/api/v1/clans/recovery").json()["governance"]
    assert all(x["existing_case_id"] == first.json()["id"] for x in preflight["removal_targets"])
    assert all(x["blocked_reason"] == conflict.json()["code"] for x in preflight["removal_targets"])
    forbidden = governance_http(target.player).post(
        path,
        data={"target_membership_id": second.pk, "reason": "SPAM"},
        content_type="application/json",
    )
    assert (
        forbidden.status_code == 404
    )  # Existing captain lookup deliberately conceals other Clans.
    soldier = governance_http(target.player).get("/api/v1/clans/recovery").json()["governance"]
    assert soldier["dissolution"]["blocked_reason"] == "CLAN_FORBIDDEN"
    assert governance_http(clan_world[-2]()).get("/api/v1/clans/recovery").status_code == 404


def test_recovery_ended_season_and_direct_captain(clan_world, governance_http):
    now = clan_world[0]
    cs = clan_world[-1]()
    http = governance_http(cs.captain)
    assert http.get("/api/v1/clans/recovery").json()["governance"]["dissolution"]["direct"]
    cs.state = ClanSeason.State.RECOVERY
    cs.save(update_fields=("state",))
    response = http.post("/api/v1/clans/dissolution")
    assert response.status_code == 409
    assert response.json()["code"] == "CLAN_GOVERNANCE_RECOVERY_RESTRICTED"
    target = add_member(clan_world, cs)
    response = http.post(
        "/api/v1/clans/removal-cases",
        data={"target_membership_id": target.pk, "reason": "SPAM"},
        content_type="application/json",
    )
    assert response.json()["code"] == "CLAN_GOVERNANCE_RECOVERY_RESTRICTED"
    cs.state = ClanSeason.State.ENDED
    cs.save(update_fields=("state",))
    assert http.post("/api/v1/clans/dissolution").json()["code"] == "CLAN_GOVERNANCE_CLAN_ENDED"
    with pytest.raises(GovernanceConflict) as error:
        request_dissolution(cs.captain, cs, now=now + timedelta(days=50))
    assert error.value.reason == "SEASON_UNAVAILABLE"
    solo = clan_world[-1]()
    solo_http = governance_http(solo.captain)
    readiness = solo_http.get("/api/v1/clans/recovery").json()["governance"]["dissolution"]
    assert readiness["outcome"] == "IMMEDIATE"
    assert readiness["same_season_recreation_allowed"] is False
    assert solo_http.post("/api/v1/clans/dissolution").status_code == 204
    repeated = solo_http.post("/api/v1/clans/dissolution")
    assert repeated.status_code == 409
    assert repeated.json()["code"] == "CLAN_GOVERNANCE_CLAN_ENDED"
    assert solo.memberships.filter(ended_at__isnull=True).count() == 0


def test_pinned_policy_and_moderation_counts(clan_world, governance_http, phase4_config_factory):
    cs = clan_world[-1]()
    target = add_member(clan_world, cs)
    banned = add_member(clan_world, cs, state=Player.ModerationState.BANNED)
    add_member(clan_world, cs, state=Player.ModerationState.SUSPENDED)
    latest = phase4_config_factory(
        effective_at=clan_world[0] - timedelta(hours=1),
        mutate=lambda p: p["clan"]["governance"].update(minimum_eligible_voters=9),
    )
    state = governance_http(cs.captain).get("/api/v1/clans/recovery").json()["governance"]
    assert state["config_version"] != latest.version
    assert state["minimum_eligible_voters"] == 3
    assert state["dissolution"]["eligible_count"] == 2
    assert state["removal_targets"][0]["eligible_count"] == 1
    assert target.pk == state["removal_targets"][0]["target_membership_id"]
    banned_target = next(
        x for x in state["removal_targets"] if x["target_membership_id"] == banned.pk
    )
    assert banned_target["eligible_count"] == 2  # Already excluded, never subtract twice.


def test_preflight_is_rechecked_when_roster_changes(clan_world, governance_http):
    cs = clan_world[-1]()
    http = governance_http(cs.captain)
    assert (
        http.get("/api/v1/clans/recovery").json()["governance"]["dissolution"]["outcome"]
        == "IMMEDIATE"
    )
    add_member(clan_world, cs)
    response = http.post("/api/v1/clans/dissolution")
    assert response.status_code == 409
    assert response.json()["code"] == "CLAN_GOVERNANCE_INSUFFICIENT_VOTERS"
    cs.refresh_from_db()
    assert cs.state == ClanSeason.State.ACTIVE
    assert cs.memberships.filter(ended_at__isnull=True).count() == 2


@pytest.mark.parametrize("same_type", [True, False])
def test_concurrent_cases_preserve_existing_per_type_rules(clan_world, same_type):
    assert connection.vendor == "postgresql"
    cs = clan_world[-1]()
    targets = [add_member(clan_world, cs) for _ in range(4)]
    barrier = Barrier(2)

    def run(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if not same_type and index:
                case = request_dissolution(cs.captain, cs)
                assert case is not None
                return case.pk
            return open_case(
                initiator=cs.captain,
                clan_season=cs,
                case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
                reason="SPAM",
                target=targets[index],
            ).pk
        except GovernanceConflict as error:
            return error.reason
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert ClanGovernanceCase.objects.filter(clan_season=cs).count() == (1 if same_type else 2)
    assert results.count("OPEN_CASE_EXISTS") == (1 if same_type else 0)
