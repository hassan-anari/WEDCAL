from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.clans.economy import canonical_clan_projection
from apps.clans.models import ClanEconomicSettlement
from apps.progression.models import PlayerProgression
from apps.rewards.models import RewardLedger
from apps.seasons.close import freeze_standings, reconcile_clan_projections, reconcile_season_points
from apps.seasons.models import PlayerSeason, PlayerSeasonFinalStanding


@pytest.mark.django_db
def test_equal_points_with_stale_player_tie_is_repaired(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    now = timezone.now()
    config = phase4_config_factory(effective_at=now - timedelta(days=2))
    season = season_factory(config, starts_at=now - timedelta(days=1))
    player = player_factory(991001)
    occurred_at = now - timedelta(hours=2)
    RewardLedger.objects.create(
        player=player,
        season=season,
        source_type="TEST",
        source_id="tie",
        context="TEST",
        gross_amount=10,
        player_amount=10,
        config_version=config,
        idempotency_key="p11-player-tie",
        occurred_at=occurred_at,
    )
    PlayerSeason.objects.create(player=player, season=season, season_points=10)
    PlayerProgression.objects.create(
        player=player,
        lifetime_points=10,
        level=1,
        dexo_stage="settler",
        config_version=config,
    )

    report = reconcile_season_points(season, repair=True)

    assert report["numeric_drifted_players"] == 0
    assert report["tie_provenance_drifted_players"] == 1
    assert PlayerSeason.objects.get(player=player, season=season).score_reached_at == occurred_at


@pytest.mark.django_db
def test_positive_player_without_tie_cannot_freeze(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    PlayerSeason.objects.create(
        player=player_factory(991002), season=season, season_points=1, score_reached_at=None
    )
    with pytest.raises(ValidationError, match="tie provenance"):
        freeze_standings(season)


@pytest.mark.django_db
def test_corrected_player_tie_order_is_frozen(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    now = timezone.now()
    config = phase4_config_factory(effective_at=now - timedelta(days=2))
    season = season_factory(config, starts_at=now - timedelta(days=1))
    for index, reached_at in enumerate((now, now - timedelta(hours=1))):
        player = player_factory(991010 + index)
        RewardLedger.objects.create(
            player=player,
            season=season,
            source_type="TEST",
            source_id=str(index),
            context="TEST",
            gross_amount=10,
            player_amount=10,
            config_version=config,
            idempotency_key=f"p11-order-{index}",
            occurred_at=reached_at,
        )
        PlayerSeason.objects.create(player=player, season=season, season_points=10)
    reconcile_season_points(season, repair=True)
    freeze_standings(season)
    rows = list(PlayerSeasonFinalStanding.objects.order_by("final_rank"))
    assert rows[0].score_reached_at < rows[1].score_reached_at


@pytest.mark.django_db
def test_clan_score_and_tie_are_rebuilt_from_canonical_sources(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, season, _, _, clan = clan_world
    clan_season = clan()
    ClanEconomicSettlement.objects.create(
        clan_season=clan_season,
        membership=clan_season.memberships.get(),
        source_type="TEST",
        source_id="canonical",
        gross_amount=7,
        player_amount=7,
        treasury_amount=0,
        contribution_amount=0,
        clan_score_amount=7,
        config_version=config,
        idempotency_key="p11-clan-rebuild",
        occurred_at=now,
    )
    clan_season.competitive_score = 99
    clan_season.score_reached_at = now - timedelta(days=1)
    clan_season.save(update_fields=("competitive_score", "score_reached_at"))
    report = reconcile_clan_projections(season, repair=True)
    clan_season.refresh_from_db()
    assert report["drifted_clans"] == report["repaired_clans"] == 1
    assert clan_season.competitive_score == 7
    assert clan_season.score_reached_at == now
    assert all(
        getattr(clan_season, key) == value
        for key, value in canonical_clan_projection(clan_season).items()
    )


@pytest.mark.django_db
def test_clan_reconciliation_command_delegates_complete_canonical_rebuild(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, season, _, _, clan = clan_world
    clan_season = clan()
    clan_season.capacity_bonus = 999
    clan_season.hq_level = 999
    clan_season.save(update_fields=("capacity_bonus", "hq_level"))
    User.objects.create_user("clan-operator")

    call_command(
        "reconcile_clan_economy",
        season.code,
        repair=True,
        actor="clan-operator",
        reason="focused canonical rebuild test",
    )

    clan_season.refresh_from_db()
    expected = canonical_clan_projection(clan_season)
    assert all(getattr(clan_season, key) == value for key, value in expected.items())
