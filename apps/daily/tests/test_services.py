# mypy: disable-error-code="no-untyped-def"
from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread
from unittest.mock import patch

import pytest
from django.db import close_old_connections

from apps.daily.models import DailyClaim
from apps.daily.services import claim_daily
from apps.progression.models import PlayerProgression
from apps.rewards.models import RewardLedger
from apps.seasons.models import PlayerSeason, Season

BASE = datetime(2026, 1, 2, tzinfo=UTC)


@pytest.mark.django_db
def test_daily_streak_cycle_reset_boundary_and_accounting(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    claims = [claim_daily(player, BASE + timedelta(days=day))[0] for day in range(8)]
    assert [(row.streak_count_after_claim, row.cycle_day) for row in claims[:2]] == [(1, 1), (2, 2)]
    assert (claims[6].streak_count_after_claim, claims[6].cycle_day) == (7, 7)
    assert (claims[7].streak_count_after_claim, claims[7].cycle_day) == (8, 1)
    assert claims[0].reward_amount == config.payload["daily"]["reward_cycle_points"][0]
    assert claims[6].reward_amount == config.payload["daily"]["reward_cycle_points"][6]
    assert claims[0].period_start == BASE
    assert claims[0].season == season and claims[0].config_version == config
    total = sum(row.reward_amount for row in claims)
    assert RewardLedger.objects.filter(player=player, source_type="DAILY").count() == 8
    assert PlayerProgression.objects.get(player=player).lifetime_points == total
    assert PlayerSeason.objects.get(player=player, season=season).season_points == total


@pytest.mark.django_db
def test_daily_missed_period_resets_streak(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    claim_daily(player, BASE)
    missed, _ = claim_daily(player, BASE + timedelta(days=2))
    assert (missed.streak_count_after_claim, missed.cycle_day) == (1, 1)


@pytest.mark.django_db
def test_daily_non_midnight_reset_and_exact_boundary(
    phase4_config_factory, player_factory, season_factory
) -> None:
    def mutate(payload):
        payload["daily"]["reset_hour"] = 6
        payload["daily"]["reset_minute"] = 30

    config = phase4_config_factory(mutate=mutate)
    season_factory(config)
    player = player_factory()
    before, _ = claim_daily(player, datetime(2026, 1, 2, 6, 29, 59, tzinfo=UTC))
    boundary, _ = claim_daily(player, datetime(2026, 1, 2, 6, 30, tzinfo=UTC))
    assert before.period_start == datetime(2026, 1, 1, 6, 30, tzinfo=UTC)
    assert boundary.period_start == datetime(2026, 1, 2, 6, 30, tzinfo=UTC)
    assert boundary.streak_count_after_claim == 2


@pytest.mark.django_db
def test_daily_sequential_retry_grants_once(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    first, created = claim_daily(player, BASE)
    retry, retry_created = claim_daily(player, BASE)
    assert created and not retry_created and retry.pk == first.pk
    assert DailyClaim.objects.count() == RewardLedger.objects.count() == 1
    assert PlayerProgression.objects.get(player=player).lifetime_points == first.reward_amount


@pytest.mark.django_db(transaction=True)
def test_daily_concurrent_claim_grants_once_without_database_error(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    barrier = Barrier(2)
    results: list[tuple[int, bool]] = []
    errors: list[BaseException] = []

    def run() -> None:
        close_old_connections()
        try:
            barrier.wait()
            row, created = claim_daily(player, BASE)
            results.append((row.pk, created))
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(results) == 2 and len({pk for pk, _ in results}) == 1
    assert DailyClaim.objects.count() == RewardLedger.objects.count() == 1
    assert (
        PlayerProgression.objects.get(player=player).lifetime_points
        == DailyClaim.objects.get().reward_amount
    )


@pytest.mark.django_db
def test_daily_reward_failure_rolls_back_everything(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory()
    with (
        patch("apps.daily.services.settle_player_reward", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        claim_daily(player, BASE)
    assert not DailyClaim.objects.exists()
    assert not RewardLedger.objects.exists()
    assert not PlayerProgression.objects.exists()
    assert not PlayerSeason.objects.exists()


@pytest.mark.django_db
def test_daily_missing_season_or_unavailable_config_has_no_side_effects(
    phase4_config_factory, player_factory
) -> None:
    phase4_config_factory()
    player = player_factory()
    with pytest.raises(LookupError):
        claim_daily(player, BASE)
    assert not DailyClaim.objects.exists() and not RewardLedger.objects.exists()
    Season.objects.all().delete()
