from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection

from apps.arena.epochs import (
    ScoringPolicyUnconfigured,
    epoch_for_source_time,
    initialize_epochs,
    settle_epoch,
)
from apps.arena.models import EconomicEpoch, QualificationDecision
from apps.arena.qualification import is_player_qualified, record_qualification_decision
from apps.arena.services import player_ranking_queryset
from apps.game_config.services import resolve_game_config_version
from apps.players.models import Player
from apps.rewards.services import settle_player_reward
from apps.seasons.models import PlayerSeason
from dexo_land.api import api

AT = datetime(2026, 1, 2, tzinfo=UTC)


def test_phase9_openapi_and_locked_warfront_only() -> None:
    paths = api.get_openapi_schema()["paths"]
    assert "/api/v1/arena/rankings/players" in paths
    assert "/api/v1/arena/rankings/clans" in paths
    assert "/api/v1/arena/state" in paths
    assert "/api/v1/arena/dlx" in paths
    assert all("/war" not in path.lower() for path in paths)


@pytest.mark.django_db
def test_qualification_is_authoritative_versioned_and_fail_closed(player_factory) -> None:  # type: ignore[no-untyped-def]
    player = player_factory()
    assert not is_player_qualified(player, policy_key="QUALIFIED_PLAYER", policy_version=1, at=AT)
    row, created = record_qualification_decision(
        player=player,
        policy_key="QUALIFIED_PLAYER",
        policy_version=1,
        decision=QualificationDecision.Decision.QUALIFIED,
        effective_at=AT,
        reason_code="AUTHORITATIVE_REVIEW",
        source="FIXTURE",
        source_id="qp-1",
    )
    assert created and is_player_qualified(
        player, policy_key="QUALIFIED_PLAYER", policy_version=1, at=AT
    )
    duplicate, created = record_qualification_decision(
        player=player,
        policy_key="QUALIFIED_PLAYER",
        policy_version=1,
        decision=QualificationDecision.Decision.QUALIFIED,
        effective_at=AT,
        reason_code="AUTHORITATIVE_REVIEW",
        source="FIXTURE",
        source_id="qp-1",
    )
    assert not created and duplicate.pk == row.pk
    assert not is_player_qualified(player, policy_key="QUALIFIED_PLAYER", policy_version=2, at=AT)
    player.moderation_state = Player.ModerationState.BANNED
    player.save(update_fields=("moderation_state",))
    assert not is_player_qualified(player, policy_key="QUALIFIED_PLAYER", policy_version=1, at=AT)


@pytest.mark.django_db
def test_player_ranking_exact_tie_order(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    players = [player_factory(300 + value) for value in (3, 1, 2)]
    reached = [AT + timedelta(seconds=value) for value in (0, 1, 1)]
    for player, timestamp in zip(players, reached, strict=True):
        PlayerSeason.objects.create(
            player=player, season=season, season_points=10, score_reached_at=timestamp
        )
    assert list(player_ranking_queryset(season).values_list("player_id", flat=True)) == [
        players[0].pk,
        players[1].pk,
        players[2].pk,
    ]


@pytest.mark.django_db
def test_six_source_time_epochs(phase4_config_factory, season_factory) -> None:  # type: ignore[no-untyped-def]
    config = phase4_config_factory()
    season = season_factory(config)
    rows = initialize_epochs(season)
    assert len(rows) == 6
    assert epoch_for_source_time(season, season.starts_at + timedelta(days=7)).epoch_number == 2
    with pytest.raises(ValidationError):
        epoch_for_source_time(season, season.starts_at + timedelta(days=42))


@pytest.mark.django_db
def test_unconfigured_scoring_fails_closed(phase4_config_factory, season_factory) -> None:  # type: ignore[no-untyped-def]
    config = phase4_config_factory()
    season = season_factory(config)
    season.settlement_start_epoch = 1
    season.save(update_fields=("settlement_start_epoch",))
    epoch = initialize_epochs(season)[0]
    epoch.state = EconomicEpoch.State.FROZEN
    epoch.save(update_fields=("state",))
    with pytest.raises(ScoringPolicyUnconfigured, match="SCORING_POLICY_UNCONFIGURED"):
        settle_epoch(epoch)
    assert not hasattr(epoch, "allocation_batch")


@pytest.mark.django_db(transaction=True)
def test_reward_ledger_database_trigger_rejects_update_and_delete(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory, monkeypatch
) -> None:
    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    resolved = resolve_game_config_version(config.pk)
    ledger, _ = settle_player_reward(
        player=player,
        source_type="MISSION",
        source_id="append-only",
        context="MISSION",
        amount=1,
        idempotency_key="append-only",
        occurred_at=AT,
        resolved_config=resolved,
    )
    with pytest.raises(DatabaseError), connection.cursor() as cursor:
        cursor.execute("UPDATE rewards_rewardledger SET player_amount=2 WHERE id=%s", [ledger.pk])
    connection.rollback()
    with pytest.raises(DatabaseError), connection.cursor() as cursor:
        cursor.execute("DELETE FROM rewards_rewardledger WHERE id=%s", [ledger.pk])
