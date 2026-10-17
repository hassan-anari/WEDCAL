# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from apps.arena import services as arena
from apps.arena.models import RankingProjectionEvent
from apps.clans.api import applicants
from apps.clans.economy import activate_item, purchase_item, settle_clan_reward
from apps.clans.models import ClanMembershipEvent, ClanRole
from apps.clans.services import request_join
from apps.clans.tests.test_b5_integration import b5_http as b5_http
from apps.daily.services import claim_daily, current_streaks, get_daily_state

pytestmark = pytest.mark.django_db(transaction=True)


def test_applicant_real_arena_ranks_daily_streaks_constant_queries(
    clan_world, monkeypatch, b5_http
):
    now, _, season, resolved, player, clan = clan_world
    cache.clear()
    cs = clan()
    people = [player(ClanRole.SOLDIER) for _ in range(5)]
    for p in people:
        request_join(p, cs, resolved)
    for p in people[:2]:
        claim_daily(p, now)
    arena.consume_pending_events()
    arena.rebuild_ranking(season, "PLAYER")
    request = RequestFactory().get("/api/v1/clans/applicants")
    request.auth = cs.captain  # type: ignore[attr-defined]
    real = arena._redis()
    calls = []

    class ObservedRedis:
        def eval(self, *args):
            calls.append(args)
            return real.eval(*args)

    monkeypatch.setattr(arena, "_redis", lambda: ObservedRedis())
    with CaptureQueriesContext(connection) as small:
        one = applicants(request, limit=1)
    calls.clear()
    with CaptureQueriesContext(connection) as large:
        many = applicants(request, limit=100)
    assert len(calls) == 1
    assert len(large) <= len(small) + 1
    assert [r.rank_status for r in many] == ["RANKED", "RANKED", "UNRANKED", "UNRANKED", "UNRANKED"]
    assert [r.rank for r in many[:2]] == [1, 2] and one[0].rank == 1
    assert [r.current_streak for r in many] == [1, 1, 0, 0, 0]
    for p in people:
        assert current_streaks([p], now, resolved)[p.pk] == get_daily_state(p, now, resolved).streak
    http = b5_http(cs.captain)
    published = {"ready": http.get("/api/v1/clans/applicants").json()}
    # Redis downtime is explicitly different from a genuine unranked Player.
    monkeypatch.setattr(arena, "_redis", lambda: (_ for _ in ()).throw(ConnectionError()))
    monkeypatch.setattr(arena, "dispatch_rebuild_safely", lambda *_: None)
    assert {r.rank_status for r in applicants(request)} == {"UNAVAILABLE"}
    published["unavailable"] = http.get("/api/v1/clans/applicants").json()
    RankingProjectionEvent.objects.create(
        season=season,
        entity_type="PLAYER",
        entity_id=people[0].pk,
        score=80,
        score_reached_at=now,
        operation="UPSERT",
        generation=2,
        source_id="b6-pending",
    )
    assert {r.rank_status for r in applicants(request)} == {"RECONCILING"}
    published["reconciling"] = http.get("/api/v1/clans/applicants").json()
    if os.getenv("B6_EXPORT_FIXTURES"):
        Path("docs/fixtures/b6/applicants-http.json").write_text(
            json.dumps(published, indent=2) + "\n"
        )


def test_daily_streak_batch_handles_missed_periods(clan_world):
    now, _, _, resolved, player, _ = clan_world
    p = player()
    claim_daily(p, now)
    for at in [now, now + timedelta(days=1), now + timedelta(days=2)]:
        assert current_streaks([p], at, resolved)[p.pk] == get_daily_state(p, at, resolved).streak


def test_private_activity_feed_stable_pagination_no_duplicated_treasury(clan_world, b5_http):
    now, _, _, resolved, player, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    settlement, _ = settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id="b6-durable-fixture",
        gross_amount=100000,
        idempotency_key="b6-durable-fixture",
        occurred_at=now,
        resolved_config=resolved,
    )
    purchase_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="rally-beacon",
        idempotency_key="b6-purchase",
        resolved_config=resolved,
    )
    activate_item(
        clan_season=cs,
        actor=cs.captain,
        item_id="rally-beacon",
        idempotency_key="b6-use",
        at=now,
        resolved_config=resolved,
    )
    client = b5_http(cs.captain)
    response = client.get("/api/v1/clans/activity", {"limit": 2})
    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "private, no-store"
    page = response.json()
    all_items = page["items"][:]
    pages = [page]
    while page["next_cursor"]:
        response = client.get("/api/v1/clans/activity", {"limit": 2, "cursor": page["next_cursor"]})
        assert response.status_code == 200
        page = response.json()
        pages.append(page)
        all_items += page["items"]
    assert len({r["id"] for r in all_items}) == len(all_items) == 4
    assert {r["event_type"] for r in all_items} == {
        "MEMBER_JOINED",
        "CLAN_REWARD_SETTLED",
        "ITEM_PURCHASED",
        "ITEM_ACTIVATED",
    }
    reward = next(r for r in all_items if r["event_type"] == "CLAN_REWARD_SETTLED")
    assert reward["amount"] == settlement.player_amount
    assert reward["treasury_amount"] == settlement.treasury_amount
    assert client.get("/api/v1/clans/treasury").json()["history"]
    assert b5_http(player(ClanRole.SOLDIER)).get("/api/v1/clans/activity").status_code == 404
    other = clan()
    assert (
        b5_http(other.captain)
        .get("/api/v1/clans/activity", {"cursor": pages[0]["next_cursor"]})
        .status_code
        == 422
    )
    assert client.get("/api/v1/clans/activity", {"cursor": "tampered"}).status_code == 422
    for limit in [0, 51, -1]:
        assert client.get("/api/v1/clans/activity", {"limit": limit}).status_code == 422
    serialized = json.dumps(all_items)
    assert not any(
        s in serialized for s in ["telegram_user_id", "invite_link", "source_id", "message_text"]
    )
    if os.getenv("B6_EXPORT_FIXTURES"):
        Path("docs/fixtures/b6").mkdir(parents=True, exist_ok=True)
        Path("docs/fixtures/b6/activity-http.json").write_text(json.dumps(pages, indent=2) + "\n")


def test_clan_standing_matches_arena_and_discovery_batches_redis(clan_world, b5_http, monkeypatch):
    now, _, season, resolved, _, clan = clan_world
    cache.clear()
    first, second = clan(), clan()
    for cs in [first, second]:
        member = cs.memberships.get()
        ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    settle_clan_reward(
        membership=first.memberships.get(),
        source_type="CLAN_ACTIVITY",
        source_id="standing-three",
        gross_amount=3,
        idempotency_key="standing-three",
        occurred_at=now,
        resolved_config=resolved,
    )
    arena.consume_pending_events()
    assert arena.rebuild_ranking(season, "CLAN")
    client = b5_http(first.captain)
    canonical = client.get("/api/v1/arena/rankings/clans").json()
    assert canonical["own_standing"]["rank"] == 1
    home = client.get("/api/v1/clans/home").json()
    detail = client.get(f"/api/v1/clans/{first.clan_id}").json()
    assert home["rank"] == detail["rank"] == 1
    assert home["rank_status"] == detail["rank_status"] == "RANKED"
    real = arena._redis()
    calls = []

    class ObservedRedis:
        def eval(self, *args):
            calls.append(args)
            return real.eval(*args)

    monkeypatch.setattr(arena, "_redis", lambda: ObservedRedis())
    discover = client.get("/api/v1/clans/").json()
    assert len(calls) == 1
    assert {c["id"]: c["rank_status"] for c in discover} == {
        first.clan_id: "RANKED",
        second.clan_id: "UNRANKED",
    }
    # A projection with stale generations must not claim a valid standing.
    from apps.clans.models import ClanSeason

    ClanSeason.objects.filter(pk=first.pk).update(ranking_generation=99)
    monkeypatch.setattr(arena, "dispatch_rebuild_safely", lambda *_: None)
    stale = client.get("/api/v1/clans/home").json()
    assert stale["rank"] is None and stale["rank_status"] == "RECONCILING"
    if os.getenv("B6_EXPORT_FIXTURES"):
        Path("docs/fixtures/b6/clan-standing-http.json").write_text(
            json.dumps(
                {"home": home, "detail": detail, "discovery": discover, "reconciling": stale},
                indent=2,
            )
            + "\n"
        )
