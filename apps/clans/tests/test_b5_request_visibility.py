# mypy: disable-error-code="no-untyped-def,no-untyped-call"
"""Signed HTTP/PostgreSQL coverage of complete owner-only application visibility."""

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.clans.models import ClanInvite, ClanJoinRequest, ClanMembership, ClanRole, ClanSeason
from apps.clans.services import decide_request, reconcile_telegram_membership, request_join
from apps.seasons.models import Season

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def synthetic_only(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_kw: pytest.fail("No live Telegram")
    )
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda _pk: None)


@pytest.fixture
def http(init_data_factory, settings):
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"

    def client(player):
        return Client(
            HTTP_AUTHORIZATION="tma "
            + init_data_factory(user={"id": player.telegram_user_id, "first_name": "Soldier"})
        )

    return client


def older_clan_season(world, current):
    now, config, *_ = world
    old = Season.objects.create(
        code="synthetic-older-reservation",
        title="Synthetic older Season",
        starts_at=now - timedelta(days=100),
        ends_at=now - timedelta(days=55),
        baseline_config=config,
    )
    return ClanSeason.objects.create(
        clan=current.clan,
        season=old,
        captain=current.captain,
        base_capacity=20,
        config_version=config,
    )


@pytest.mark.parametrize(
    "query,history_count",
    [("", 20), ("?limit=1", 1), ("?limit=100", 26), ("?limit=1&offset=999", 0)],
)
def test_accepted_reservation_survives_26_newer_terminal_rows(
    clan_world, http, query, history_count
):
    _, _, season, resolved, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    accepted = decide_request(cs.captain, request_join(soldier, cs, resolved).pk, accept=True)
    ClanInvite.objects.filter(join_request=accepted).update(
        state="REVOKE_FAILED",
        desired_terminal_state="REVOKED",
        unknown_credential_risk=True,
        invite_link="https://t.me/+PRIVATE_SYNTHETIC",
    )
    terminals = ClanJoinRequest.objects.bulk_create(
        [ClanJoinRequest(applicant=soldier, clan_season=cs, state="DECLINED") for _ in range(26)]
    )
    client = http(soldier)
    response = client.get("/api/v1/clans/join-requests/current" + query)
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1 + history_count
    active = [r for r in rows if r["is_active"]]
    assert [r["id"] for r in active] == [accepted.pk]
    row = rows[0]
    assert row["clan_season_id"] == cs.pk and row["season_id"] == season.pk
    assert row["season_code"] == season.code and row["is_current_season"]
    assert row["state"] == "ACCEPTED" and row["reserves_capacity"]
    assert row["credential_unresolved"] and row["cancellation_pending"]
    assert row["blocks_new_application"] and row["handling"] == "CURRENT_SEASON"
    assert all(r["handling"] == "HISTORY" and not r["blocks_new_application"] for r in rows[1:])
    if history_count:
        assert rows[1]["id"] == terminals[-1].pk
    assert "PRIVATE_SYNTHETIC" not in response.content.decode()
    assert response["Cache-Control"] == "private, no-store"
    if os.getenv("B5_EXPORT_FIXTURES") == "1" and query == "":
        Path("docs/fixtures/b5/request-history-isolation-http.json").write_text(
            json.dumps({"newer_terminal_count": 26, "default_response": rows}, indent=2) + "\n"
        )
    # DELETE does not release a failed/uncertain revocation reservation.
    assert client.delete(f"/api/v1/clans/join-requests/{accepted.pk}").status_code == 200
    accepted.refresh_from_db()
    assert accepted.state == "ACCEPTED"


def test_older_season_and_all_legacy_overlaps_are_visible_without_selecting_a_winner(
    clan_world, http
):
    _, _, _, _, player, clan = clan_world
    soldier, first, second = player(ClanRole.SOLDIER), clan(), clan()
    old_cs = older_clan_season(clan_world, first)
    old = ClanJoinRequest.objects.create(applicant=soldier, clan_season=old_cs, state="ACCEPTED")
    ClanInvite.objects.create(
        join_request=old, state="ACTIVE", invite_link="https://t.me/+PRIVATE_OLD"
    )
    pending = ClanJoinRequest.objects.create(applicant=soldier, clan_season=first)
    extra = ClanJoinRequest.objects.create(applicant=soldier, clan_season=second)
    # Another player's active path/credential must never appear in this response.
    other = player(ClanRole.SOLDIER)
    secret = ClanJoinRequest.objects.create(applicant=other, clan_season=second, state="ACCEPTED")
    ClanInvite.objects.create(
        join_request=secret, state="ACTIVE", invite_link="https://t.me/+OTHER_SECRET"
    )
    client = http(soldier)
    response = client.get("/api/v1/clans/join-requests/current?limit=1&offset=999")
    assert response.status_code == 200
    rows = response.json()
    assert {r["id"] for r in rows} == {old.pk, pending.pk, extra.pk}
    assert all(r["is_active"] and r["blocks_new_application"] for r in rows)
    old_row = next(r for r in rows if r["id"] == old.pk)
    assert not old_row["is_current_season"] and old_row["season_id"] == old_cs.season_id
    assert old_row["handling"] == "OPERATOR_REQUIRED" and old_row["reserves_capacity"]
    assert sum(r["reserves_capacity"] for r in rows) == 1
    # Generic invite lookup must not choose a legacy winner or return old credentials.
    conflict = client.get("/api/v1/clans/invite/current")
    assert conflict.status_code == 409 and conflict.json()["code"] == "CLAN_STATE_CONFLICT"
    assert "PRIVATE_OLD" not in response.content.decode() + conflict.content.decode()
    assert "OTHER_SECRET" not in response.content.decode() + conflict.content.decode()
    assert ClanJoinRequest.objects.get(pk=old.pk).state == "ACCEPTED"
    assert ClanJoinRequest.objects.filter(applicant=soldier, state="PENDING").count() == 2
    if os.getenv("B5_EXPORT_FIXTURES") == "1":
        Path("docs/fixtures/b5/request-visibility-http.json").write_text(
            json.dumps(
                {"all_active_despite_history_offset": rows, "ambiguous_invite": conflict.json()},
                indent=2,
            )
            + "\n"
        )


def test_more_than_100_legacy_pending_rows_are_not_capped(clan_world, http):
    _, _, _, _, player, clan = clan_world
    soldier = player(ClanRole.SOLDIER)
    expected = {
        ClanJoinRequest.objects.create(applicant=soldier, clan_season=clan()).pk for _ in range(101)
    }
    rows = http(soldier).get("/api/v1/clans/join-requests/current?limit=1&offset=1000").json()
    assert {r["id"] for r in rows} == expected
    assert all(r["is_active"] and not r["reserves_capacity"] for r in rows)


@pytest.mark.parametrize("state", ["CANCELLED", "ACTIVATED", "DECLINED"])
def test_terminal_label_cannot_paginate_unsafe_old_credential_away(clan_world, http, state):
    _, _, _, _, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    old_cs = older_clan_season(clan_world, cs)
    old = ClanJoinRequest.objects.create(applicant=soldier, clan_season=old_cs, state=state)
    ClanInvite.objects.create(join_request=old, state="MANUAL_REVIEW", unknown_credential_risk=True)
    rows = http(soldier).get("/api/v1/clans/join-requests/current?offset=999").json()
    assert len(rows) == 1 and rows[0]["id"] == old.pk
    assert not rows[0]["is_active"] and not rows[0]["reserves_capacity"]
    assert rows[0]["credential_unresolved"] and rows[0]["blocks_new_application"]
    assert rows[0]["handling"] == "OPERATOR_REQUIRED"
    old.refresh_from_db()
    assert old.state == state


def test_older_accepted_alone_is_informational_and_invite_lookup_cannot_issue(clan_world, http):
    _, _, _, _, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    old_cs = older_clan_season(clan_world, cs)
    old = ClanJoinRequest.objects.create(applicant=soldier, clan_season=old_cs, state="ACCEPTED")
    invitation = ClanInvite.objects.create(join_request=old)
    client = http(soldier)
    rows = client.get("/api/v1/clans/join-requests/current").json()
    assert len(rows) == 1 and rows[0]["handling"] == "OPERATOR_REQUIRED"
    assert client.get("/api/v1/clans/invite/current").status_code == 409
    invitation.refresh_from_db()
    assert invitation.state == "PENDING" and invitation.attempt_count == 0
    # Merely displaying an old reservation cannot extend its activation window.
    reconcile_telegram_membership(
        player=soldier,
        group=cs.clan.telegram_group,
        present=True,
        telegram_update_id=955001,
        observed_at=clan_world[0],
    )
    assert not ClanMembership.objects.filter(player=soldier).exists()
    old.refresh_from_db()
    assert old.state == "ACCEPTED" and old.clan_season_id == old_cs.pk


def test_active_rows_remain_readable_without_a_current_season(
    clan_world, http, close_season_for_test
):
    _, _, season, _, player, clan = clan_world
    soldier, cs = player(ClanRole.SOLDIER), clan()
    req = ClanJoinRequest.objects.create(applicant=soldier, clan_season=cs)
    close_season_for_test(season)
    response = http(soldier).get("/api/v1/clans/join-requests/current")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["id"] == req.pk and not row["is_current_season"]
    assert row["handling"] == "OPERATOR_REQUIRED"


def test_history_pagination_is_separate_and_owner_authentication_is_required(clan_world, http):
    _, _, _, _, player, clan = clan_world
    soldier, other, cs = player(ClanRole.SOLDIER), player(ClanRole.SOLDIER), clan()
    pending = ClanJoinRequest.objects.create(applicant=soldier, clan_season=cs)
    history = ClanJoinRequest.objects.bulk_create(
        [ClanJoinRequest(applicant=soldier, clan_season=cs, state="DECLINED") for _ in range(5)]
    )
    client = http(soldier)
    with CaptureQueriesContext(connection) as queries:
        response = client.get("/api/v1/clans/join-requests/current?limit=2&offset=2")
    assert response.status_code == 200
    assert [r["id"] for r in response.json()] == [pending.pk, history[2].pk, history[1].pk]
    # One application SQL, including both slices and related metadata; no N+1.
    application_queries = [q for q in queries if 'FROM "clans_clanjoinrequest"' in q["sql"]]
    assert len(application_queries) == 1
    assert http(other).get("/api/v1/clans/join-requests/current").json() == []
    assert Client().get("/api/v1/clans/join-requests/current").status_code == 401
    assert http(other).delete(f"/api/v1/clans/join-requests/{pending.pk}").status_code == 404


def test_current_season_overlap_cannot_implicitly_select_an_invite(clan_world, http):
    _, _, _, resolved, player, clan = clan_world
    soldier, cs, extra_cs = player(ClanRole.SOLDIER), clan(), clan()
    accepted = decide_request(cs.captain, request_join(soldier, cs, resolved).pk, accept=True)
    ClanInvite.objects.filter(join_request=accepted).update(
        state="ACTIVE", invite_link="https://t.me/+OWN_SYNTHETIC"
    )
    extra = ClanJoinRequest.objects.create(applicant=soldier, clan_season=extra_cs)
    client = http(soldier)
    rows = client.get("/api/v1/clans/join-requests/current?limit=1").json()
    assert {r["id"] for r in rows if r["is_active"]} == {accepted.pk, extra.pk}
    assert all(r["is_current_season"] for r in rows)
    assert client.get("/api/v1/clans/invite/current").status_code == 409
    assert client.delete(f"/api/v1/clans/join-requests/{extra.pk}").status_code == 200
    response = client.get("/api/v1/clans/invite/current")
    assert response.status_code == 200
    assert response.json()["invite_link"] == "https://t.me/+OWN_SYNTHETIC"
    accepted.refresh_from_db()
    assert accepted.state == "ACCEPTED"
