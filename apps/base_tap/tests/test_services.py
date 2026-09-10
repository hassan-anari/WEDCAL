from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db import close_old_connections

from apps.base_tap.models import BaseTapBatch, BaseTapDailyAccumulator
from apps.base_tap.services import recover_pending_tap_batches, submit_tap_batch
from apps.progression.models import PlayerProgression
from apps.referrals.models import ReferralShareOutcome
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
from apps.seasons.models import PlayerSeason, Season


@pytest.mark.django_db
def test_batch_retry_is_idempotent_and_aggregated(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    at = datetime(2026, 1, 2, tzinfo=UTC)
    config = phase4_config_factory(
        effective_at=at - timedelta(days=1),
        mutate=lambda payload: payload["base_tap"].update(min_batch_interval_ms=0),
    )
    season = season_factory(config, starts_at=at - timedelta(hours=1))
    player = player_factory()
    first = submit_tap_batch(player, tap_count=40, idempotency_key="batch-1", at=at)
    replay = submit_tap_batch(player, tap_count=40, idempotency_key="batch-1", at=at)
    assert first.created is True and replay.created is False
    assert first.batch.accepted_taps == 40 and first.batch.earned_points == 40
    assert BaseTapBatch.objects.count() == 1
    assert BaseTapDailyAccumulator.objects.get().accepted_taps == 40
    reward = RewardLedger.objects.get(source_type="BASE_TAP")
    assert reward.player_amount == 40 and reward.season == season
    assert reward.config_version == config
    assert PlayerProgression.objects.get(player=player).lifetime_points == 40
    assert PlayerSeason.objects.get(player=player, season=season).season_points == 40
    assert not RewardSettlementOutbox.objects.filter(reward=reward).exists()
    assert not ReferralShareOutcome.objects.filter(source_reward=reward).exists()


@pytest.mark.django_db
def test_utc_day_boundary_uses_separate_bounded_accumulators(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    before = datetime(2026, 1, 2, 23, 59, 59, tzinfo=UTC)
    config = phase4_config_factory(effective_at=before - timedelta(days=1))
    season_factory(config, starts_at=before - timedelta(days=1), ends_at=before + timedelta(days=2))
    player = player_factory()
    one = submit_tap_batch(player, tap_count=50, idempotency_key="day-one", at=before)
    two = submit_tap_batch(
        player, tap_count=50, idempotency_key="day-two", at=before + timedelta(seconds=1)
    )
    assert one.batch.accepted_taps == two.batch.accepted_taps == 50
    assert BaseTapDailyAccumulator.objects.count() == 2
    assert RewardLedger.objects.filter(source_type="BASE_TAP").count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_batches_cannot_exceed_postgres_daily_cap(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    at = datetime(2026, 1, 2, tzinfo=UTC)
    config = phase4_config_factory(
        effective_at=at - timedelta(days=1),
        mutate=lambda payload: payload["base_tap"].update(min_batch_interval_ms=0),
    )
    season_factory(config, starts_at=at - timedelta(hours=1))
    player = player_factory()

    def submit(key: str) -> int:
        close_old_connections()
        try:
            return submit_tap_batch(
                player, tap_count=40, idempotency_key=key, at=at
            ).batch.accepted_taps
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(submit, ("concurrent-a", "concurrent-b")))
    assert sum(accepted) == 50
    assert BaseTapDailyAccumulator.objects.get().accepted_taps == 50
    assert (
        sum(
            RewardLedger.objects.filter(source_type="BASE_TAP").values_list(
                "player_amount", flat=True
            )
        )
        == 50
    )


@pytest.mark.django_db
def test_pending_batch_recovers_after_season_close_with_original_sources(
    monkeypatch: Any, phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    at = datetime(2026, 1, 2, tzinfo=UTC)
    config = phase4_config_factory(effective_at=at - timedelta(days=1))
    season = season_factory(config, starts_at=at - timedelta(hours=1))
    player = player_factory()
    import apps.base_tap.services as services

    real_settle = getattr(services, "settle_player_reward")  # noqa: B009
    monkeypatch.setattr(
        services,
        "settle_player_reward",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("temporary")),
    )
    result = submit_tap_batch(player, tap_count=25, idempotency_key="recover", at=at)
    assert result.batch.status == BaseTapBatch.Status.PENDING and result.pending_points == 25
    season.status = Season.Status.CLOSED
    season.save(update_fields=("status",))
    monkeypatch.setattr(services, "settle_player_reward", real_settle)
    assert recover_pending_tap_batches() == 1
    reward = RewardLedger.objects.get(source_type="BASE_TAP")
    assert reward.season == season and reward.config_version == config
    assert BaseTapBatch.objects.get().status == BaseTapBatch.Status.SETTLED
