from datetime import UTC, datetime, timedelta

import pytest
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
from apps.seasons.models import PlayerSeason

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
