from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

import apps.arena.services as ranking_services
from apps.arena.models import RankingProjectionEvent
from apps.arena.services import (
    RankingProjectionUnavailable,
    acquire_rebuild_lock,
    apply_projection_event,
    promote_rebuild,
    ranking_key,
    read_ranking_slice,
    rebuild_ranking,
    release_rebuild_lock,
    renew_rebuild_lock,
)
from apps.seasons.models import (
    ClanSeasonFinalStanding,
    PlayerSeason,
    PlayerSeasonFinalStanding,
    SeasonCloseRun,
)
from apps.seasons.services import archive_closed_season

AT = datetime(2026, 1, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_redis() -> None:
    cache.clear()


@pytest.mark.django_db
def test_redis_primary_and_tie_order_and_own_rank(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    players = [player_factory(8000 + index) for index in range(4)]
    for index, player in enumerate(players):
        PlayerSeason.objects.create(
            player=player,
            season=season,
            season_points=20 if index < 3 else 10,
            score_reached_at=AT + timedelta(seconds=0 if index == 0 else 1),
            ranking_generation=1,
        )
    assert rebuild_ranking(season, RankingProjectionEvent.Entity.PLAYER)
    result = read_ranking_slice(
        season_id=season.pk,
        entity_type=RankingProjectionEvent.Entity.PLAYER,
        offset=0,
        limit=2,
        own_entity_id=players[-1].pk,
    )
    assert [entity for entity, _ in result.ranked_ids] == [players[0].pk, players[1].pk]
    assert result.own_rank == 4


@pytest.mark.django_db
def test_newer_then_older_event_cannot_regress_projection(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    older = RankingProjectionEvent.objects.create(
        season=season,
        entity_type="PLAYER",
        entity_id=player.pk,
        score=5,
        score_reached_at=AT,
        operation="UPSERT",
        source_id="old",
        generation=1,
    )
    newer = RankingProjectionEvent.objects.create(
        season=season,
        entity_type="PLAYER",
        entity_id=player.pk,
        score=9,
        score_reached_at=AT + timedelta(seconds=1),
        operation="UPSERT",
        source_id="new",
        generation=2,
    )
    assert apply_projection_event(newer)
    assert not apply_projection_event(older)
    client = cache.client.get_client(write=True)  # type: ignore[attr-defined]
    assert (
        client.zscore(
            ranking_key(season.pk, "PLAYER"),
            client.hget(f"{ranking_key(season.pk, 'PLAYER')}:members", player.pk),
        )
        == 9
    )


@pytest.mark.django_db
def test_temp_replay_does_not_ack_and_crash_is_recoverable(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    event = RankingProjectionEvent.objects.create(
        season=season,
        entity_type="PLAYER",
        entity_id=player.pk,
        score=3,
        score_reached_at=AT,
        operation="UPSERT",
        source_id="temp",
        generation=1,
    )
    apply_projection_event(event, target="arena:test:temporary", acknowledge=False)
    event.refresh_from_db()
    assert event.processed_at is None
    apply_projection_event(event)
    event.refresh_from_db()
    assert event.processed_at is not None


@pytest.mark.django_db
def test_lock_expiry_stale_owner_cannot_promote_or_release(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    token = acquire_rebuild_lock(season.pk, "PLAYER", lease_seconds=30)
    assert token and renew_rebuild_lock(season.pk, "PLAYER", token, 30)
    client = cache.client.get_client(write=True)  # type: ignore[attr-defined]
    lock = f"{ranking_key(season.pk, 'PLAYER')}:rebuild-lock"
    client.delete(lock)
    newer = acquire_rebuild_lock(season.pk, "PLAYER", lease_seconds=30)
    assert newer and newer != token
    assert not promote_rebuild(
        season_id=season.pk,
        entity_type="PLAYER",
        token=token,
        temporary_target=f"{ranking_key(season.pk, 'PLAYER')}:tmp:{token}",
    )
    assert not release_rebuild_lock(season.pk, "PLAYER", token)
    assert client.get(lock).decode() == newer


@pytest.mark.django_db
def test_redis_loss_is_fail_safe_then_rebuilds(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    PlayerSeason.objects.create(
        player=player,
        season=season,
        season_points=1,
        score_reached_at=timezone.now(),
        ranking_generation=1,
    )
    with pytest.raises(RankingProjectionUnavailable):
        read_ranking_slice(
            season_id=season.pk, entity_type="PLAYER", offset=0, limit=10, own_entity_id=player.pk
        )
    assert rebuild_ranking(season, "PLAYER")
    assert (
        read_ranking_slice(
            season_id=season.pk, entity_type="PLAYER", offset=0, limit=10, own_entity_id=player.pk
        ).own_rank
        == 1
    )


@pytest.mark.django_db
@pytest.mark.parametrize("archived", [False, True])
def test_closed_and_archived_redis_rebuild_uses_only_immutable_final_standings(
    phase4_config_factory: Any,
    season_factory: Any,
    player_factory: Any,
    close_season_for_test: Any,
    archived: bool,
) -> None:
    season = season_factory(phase4_config_factory())
    players = [player_factory(12800 + index) for index in range(2)]
    reached = [AT, AT + timedelta(seconds=1)]
    for index, player in enumerate(players):
        PlayerSeason.objects.create(
            player=player,
            season=season,
            season_points=1000 if index == 0 else 1,
            score_reached_at=reached[index],
            ranking_generation=99,
        )
        PlayerSeasonFinalStanding.objects.create(
            season=season,
            player=player,
            final_rank=index + 1,
            final_season_points=10,
            score_reached_at=reached[index],
            ranking_generation=index + 1,
            ranked_population=2,
            finalized_at=timezone.now(),
        )
    close_season_for_test(season)
    if archived:
        SeasonCloseRun.objects.create(
            season=season,
            idempotency_key=f"closed-redis:{season.pk}",
            stage=SeasonCloseRun.Stage.COMPLETED,
            cutoff_at=timezone.now(),
            service_actor="test",
            reason="closed Redis verification",
            completed_at=timezone.now(),
        )
        actor = User.objects.create_user(f"archive-{season.pk}")
        actor.user_permissions.add(Permission.objects.get(codename="close_season"))
        season = archive_closed_season(season, actor=actor, reason="archive Redis test")
    late = RankingProjectionEvent.objects.create(
        season=season,
        entity_type=RankingProjectionEvent.Entity.PLAYER,
        entity_id=players[1].pk,
        score=9999,
        score_reached_at=AT - timedelta(days=1),
        operation=RankingProjectionEvent.Operation.UPSERT,
        source_id=f"late-closed:{season.pk}",
        generation=100,
    )
    cache.clear()
    assert rebuild_ranking(season, RankingProjectionEvent.Entity.PLAYER)
    result = read_ranking_slice(
        season_id=season.pk,
        entity_type=RankingProjectionEvent.Entity.PLAYER,
        offset=0,
        limit=10,
        own_entity_id=players[1].pk,
    )
    assert result.ranked_ids == ((players[0].pk, 1), (players[1].pk, 2))
    assert result.own_rank == 2
    late.refresh_from_db()
    assert late.processed_at is None


@pytest.mark.django_db
def test_closed_clan_redis_rebuild_uses_immutable_final_standings_only(
    clan_world: Any, close_season_for_test: Any
) -> None:
    _, _, season, _, _, clan = clan_world
    clans = [clan(), clan()]
    reached = [AT, AT + timedelta(seconds=1)]
    for index, clan_season in enumerate(clans):
        clan_season.competitive_score = 1000 if index == 0 else 1
        clan_season.score_reached_at = reached[index]
        clan_season.ranking_generation = 99
        clan_season.save(
            update_fields=("competitive_score", "score_reached_at", "ranking_generation")
        )
        ClanSeasonFinalStanding.objects.create(
            season=season,
            clan_season=clan_season,
            clan=clan_season.clan,
            final_rank=index + 1,
            final_competitive_score=10,
            score_reached_at=reached[index],
            ranking_generation=index + 1,
            ranked_population=2,
            finalized_at=timezone.now(),
        )
    close_season_for_test(season)
    RankingProjectionEvent.objects.create(
        season=season,
        entity_type=RankingProjectionEvent.Entity.CLAN,
        entity_id=clans[1].pk,
        score=9999,
        score_reached_at=AT - timedelta(days=1),
        operation=RankingProjectionEvent.Operation.UPSERT,
        source_id=f"late-closed-clan:{season.pk}",
        generation=100,
    )
    cache.clear()
    assert rebuild_ranking(season, RankingProjectionEvent.Entity.CLAN)
    result = read_ranking_slice(
        season_id=season.pk,
        entity_type=RankingProjectionEvent.Entity.CLAN,
        offset=0,
        limit=10,
        own_entity_id=clans[1].pk,
    )
    assert result.ranked_ids == ((clans[0].pk, 1), (clans[1].pk, 2))
    assert result.own_rank == 2


@pytest.mark.django_db
def test_player_api_uses_redis_page_and_constant_time_own_rank(  # type: ignore[no-untyped-def]
    client: Client,
    settings,
    init_data_factory,
    telegram_bot_token,
    phase4_config_factory,
    season_factory,
    player_factory,
) -> None:
    now = timezone.now()
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    config = phase4_config_factory(effective_at=now - timedelta(days=2))
    season = season_factory(
        config, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=44)
    )
    players = [player_factory(12000 + index) for index in range(55)]
    for index, player in enumerate(players):
        PlayerSeason.objects.create(
            player=player,
            season=season,
            season_points=55 - index,
            score_reached_at=AT + timedelta(seconds=index),
            ranking_generation=1,
        )
    assert rebuild_ranking(season, "PLAYER")
    auth = init_data_factory(user={"id": players[-1].telegram_user_id, "first_name": "Last"})
    response = client.get(
        "/api/v1/arena/rankings/players?limit=2",
        headers={"Authorization": f"tma {auth}"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert [item["rank"] for item in payload["items"]] == [1, 2]
    assert payload["own_standing"]["rank"] == 55
    cache.clear()
    unavailable = client.get(
        "/api/v1/arena/rankings/players?limit=2",
        headers={"Authorization": f"tma {auth}"},
    )
    assert unavailable.status_code == 503


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("operation", "score", "expected_rank"), [("UPSERT", 9, 1), ("REMOVE", 0, None)]
)
def test_update_immediately_before_swap_is_replayed_without_loss(  # type: ignore[no-untyped-def]
    phase4_config_factory,
    season_factory,
    player_factory,
    monkeypatch,
    operation: str,
    score: int,
    expected_rank: int | None,
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    PlayerSeason.objects.create(
        player=player, season=season, season_points=5, score_reached_at=AT, ranking_generation=1
    )
    original_promote = ranking_services.promote_rebuild
    event: RankingProjectionEvent | None = None

    def racing_promote(**kwargs: object) -> bool:
        nonlocal event
        event = RankingProjectionEvent.objects.create(
            season=season,
            entity_type="PLAYER",
            entity_id=player.pk,
            score=score,
            score_reached_at=AT + timedelta(seconds=1),
            operation=operation,
            source_id=f"race-{operation}",
            generation=2,
        )
        return original_promote(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ranking_services, "promote_rebuild", racing_promote)
    assert rebuild_ranking(season, "PLAYER")
    result = read_ranking_slice(
        season_id=season.pk, entity_type="PLAYER", offset=0, limit=10, own_entity_id=player.pk
    )
    assert result.own_rank == expected_rank
    assert event is not None
    event.refresh_from_db()
    assert event.processed_at is not None
