# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from io import StringIO

import pytest
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.arena.models import RankingProjectionEvent
from apps.arena.services import (
    apply_projection_event,
    publish_clan_ranking,
    ranking_key,
    read_clan_standings,
    read_ranking_slice,
    rebuild_ranking,
)
from apps.clans.governance import cast_vote, finalize_case, request_dissolution
from apps.clans.models import ClanSeason
from apps.clans.tests.test_b5_integration import b5_http as b5_http
from apps.clans.tests.test_phase7b_governance import add_member
from apps.seasons.models import ClanSeasonFinalStanding, SeasonCloseRun
from apps.seasons.services import archive_closed_season

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def synthetic_projection(monkeypatch):
    cache.clear()
    monkeypatch.setattr("apps.arena.services.dispatch_projection_safely", lambda *_: None)
    monkeypatch.setattr("apps.arena.services.dispatch_rebuild_safely", lambda *_: None)


def scored(cs, now, score=100):
    cs.competitive_score, cs.score_reached_at, cs.ranking_generation = score, now, 1
    cs.treasury_balance = 23
    cs.save()
    event = publish_clan_ranking(cs, source_id=f"initial:{cs.pk}")
    apply_projection_event(event)
    return event


def test_singleton_durable_remove_async_pagination_and_own_standing(clan_world, b5_http):
    now, _, season, _, _, make_clan = clan_world
    dissolved, survivor, recovery = make_clan(), make_clan(), make_clan()
    for cs, score in [(dissolved, 100), (survivor, 50), (recovery, 20)]:
        scored(cs, now, score)
    recovery.state = "RECOVERY"
    recovery.save(update_fields=("state",))
    http = b5_http(dissolved.captain)
    assert http.get("/api/v1/arena/rankings/clans").json()["own_standing"]["rank"] == 1
    request_dissolution(dissolved.captain, dissolved)
    dissolved.refresh_from_db()
    event = RankingProjectionEvent.objects.get(source_id=f"clan-dissolved:{dissolved.pk}")
    assert event.operation == "REMOVE" and event.generation == 2 and event.processed_at is None
    assert (dissolved.competitive_score, dissolved.treasury_balance) == (100, 23)
    assert (
        dissolved.state == "ENDED"
        and not dissolved.memberships.filter(ended_at__isnull=True).exists()
    )
    assert http.get("/api/v1/arena/rankings/clans").status_code == 503
    apply_projection_event(event)
    first = http.get("/api/v1/arena/rankings/clans?limit=1").json()
    assert first["total_count"] == 2 and first["next_offset"] == 1
    assert first["items"][0]["clan_id"] == survivor.pk and first["items"][0]["rank"] == 1
    assert first["own_standing"] is None
    second = http.get("/api/v1/arena/rankings/clans?limit=1&offset=1").json()
    assert second["items"][0]["clan_id"] == recovery.pk and second["items"][0]["rank"] == 2
    assert second["next_offset"] is None
    own = b5_http(survivor.captain).get("/api/v1/arena/rankings/clans").json()
    assert own["own_standing"]["rank"] == 1
    assert read_clan_standings(season, [dissolved.pk])[dissolved.pk].status == "UNRANKED"


@pytest.mark.parametrize("approved", [True, False])
def test_governance_dissolution_vs_rejected_case(clan_world, approved):
    now, _, season, _, _, make_clan = clan_world
    cs = make_clan()
    old = scored(cs, now)
    for _ in range(3):
        add_member(clan_world, cs)
    case = request_dissolution(cs.captain, cs)
    assert case is not None
    for elector in case.electors.select_related("player"):
        cast_vote(case_id=case.pk, voter=elector.player, choice="YES" if approved else "NO")
    finalize_case(case.pk, now=case.voting_deadline)
    finalize_case(case.pk, now=case.voting_deadline)
    cs.refresh_from_db()
    removals = RankingProjectionEvent.objects.filter(source_id=f"clan-dissolved:{cs.pk}")
    assert removals.count() == int(approved)
    if approved:
        apply_projection_event(removals.get())
        assert cs.state == "ENDED" and cs.ranking_generation == 2
        apply_projection_event(old)  # A late UPSERT cannot resurrect the Clan.
    else:
        assert cs.state == "ACTIVE" and cs.ranking_generation == 1
    assert cs.competitive_score == 100 and cs.treasury_balance == 23
    ranking = read_ranking_slice(
        season_id=season.pk, entity_type="CLAN", offset=0, limit=20, own_entity_id=cs.pk
    )
    assert ranking.total_count == (0 if approved else 1)


def test_preexisting_ended_rebuild_and_stale_replay_after_redis_loss(clan_world, b5_http):
    now, _, season, _, _, make_clan = clan_world
    ended, live = make_clan(), make_clan()
    stale = scored(ended, now)
    scored(live, now, 20)
    # Actual legacy defect: ended without a generation bump or REMOVE event.
    ClanSeason.objects.filter(pk=ended.pk).update(state="ENDED")
    assert b5_http(live.captain).get("/api/v1/arena/rankings/clans?offset=1").status_code == 503
    assert read_clan_standings(season, [live.pk])[live.pk].rank is None
    assert rebuild_ranking(season, "CLAN")
    apply_projection_event(stale)
    result = read_ranking_slice(
        season_id=season.pk, entity_type="CLAN", offset=0, limit=20, own_entity_id=ended.pk
    )
    assert result.ranked_ids == ((live.pk, 1),) and result.total_count == 1
    cache.clear()
    apply_projection_event(stale)  # Must REMOVE even when Redis has no generation fence.
    assert rebuild_ranking(season, "CLAN")
    ended.refresh_from_db()
    new = publish_clan_ranking(ended, source_id="late-ended-publication")
    assert new.operation == "REMOVE"
    apply_projection_event(new)
    assert read_clan_standings(season, [ended.pk])[ended.pk].status == "UNRANKED"
    assert ended.competitive_score == 100


def test_dissolution_racing_rebuild_is_replayed(clan_world, monkeypatch):
    import apps.arena.services as services

    now, _, season, _, _, make_clan = clan_world
    cs = make_clan()
    scored(cs, now)
    original = services.promote_rebuild

    def promote(**kwargs):
        request_dissolution(cs.captain, cs)
        return original(**kwargs)

    monkeypatch.setattr(services, "promote_rebuild", promote)
    assert rebuild_ranking(season, "CLAN")
    result = read_ranking_slice(
        season_id=season.pk, entity_type="CLAN", offset=0, limit=20, own_entity_id=cs.pk
    )
    assert result.total_count == 0 and result.own_rank is None


@pytest.mark.parametrize("archived", [False, True])
def test_historical_snapshot_ignores_live_lifecycle(
    clan_world, close_season_for_test, b5_http, archived
):
    now, _, season, _, _, make_clan = clan_world
    cs = make_clan()
    scored(cs, now)
    ClanSeasonFinalStanding.objects.create(
        season=season,
        clan_season=cs,
        clan=cs.clan,
        final_rank=1,
        final_competitive_score=100,
        score_reached_at=now,
        ranking_generation=1,
        ranked_population=1,
        provenance={},
        finalized_at=now,
    )
    ClanSeason.objects.filter(pk=cs.pk).update(state="ENDED", competitive_score=999)
    season = close_season_for_test(season)
    if archived:
        operator = User.objects.create_superuser("archive-operator", password="test")
        SeasonCloseRun.objects.create(
            season=season,
            stage="COMPLETED",
            cutoff_at=season.ends_at,
            actor=operator,
            reason="Synthetic archive test",
            idempotency_key="synthetic-archive",
        )
        season = archive_closed_season(season, actor=operator, reason="Test archive")
    assert rebuild_ranking(season, "CLAN")
    standing = read_clan_standings(season, [cs.pk])[cs.pk]
    assert (standing.rank, standing.score, standing.status) == (1, 100, "RANKED")
    response = b5_http(cs.captain).get(f"/api/v1/arena/rankings/clans?season={season.code}")
    assert response.status_code == 200
    assert response.json()["items"][0]["competitive_score"] == 100
    assert response.json()["total_count"] == 1


def test_operator_cleanup_command_authorization_and_scope(clan_world):
    now, _, season, _, _, make_clan = clan_world
    cs = make_clan()
    scored(cs, now)
    ClanSeason.objects.filter(pk=cs.pk).update(state="ENDED")
    operator = User.objects.create_user("rebuild-operator")
    with pytest.raises(CommandError):
        call_command(
            "rebuild_live_clan_rankings",
            season=season.code,
            actor=operator.username,
            reason="Cleanup",
        )
    operator.user_permissions.add(Permission.objects.get(codename="change_rankingprojectionstate"))
    out = StringIO()
    call_command(
        "rebuild_live_clan_rankings",
        season=season.code,
        actor=operator.username,
        reason="Cleanup",
        stdout=out,
    )
    assert "eligible_population=0" in out.getvalue()
    assert cache.client.get_client(write=True).zcard(ranking_key(season.pk, "CLAN")) == 0  # type: ignore[attr-defined]
    cs.refresh_from_db()
    assert cs.competitive_score == 100
