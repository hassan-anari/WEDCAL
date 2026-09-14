from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections

from apps.base_tap.models import BaseTapDailyAccumulator, BaseTapReceipt
from apps.base_tap.services import (
    finalize_tap_accumulator,
    recover_pending_tap_accumulators,
    submit_tap_batch,
)
from apps.progression.models import PlayerProgression
from apps.referrals.models import ReferralShareOutcome
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
from apps.seasons.models import PlayerSeason


def no_rate_limit(payload: dict[str, Any]) -> None:
    payload["base_tap"]["min_batch_interval_ms"] = 0


@pytest.mark.django_db
def test_many_tiny_batches_create_one_daily_economic_settlement(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    at = datetime(2026, 1, 2, tzinfo=UTC)
    config = phase4_config_factory(effective_at=at - timedelta(days=1), mutate=no_rate_limit)
    season = season_factory(config, starts_at=at - timedelta(hours=1))
    player = player_factory()
    for number in range(50):
        result = submit_tap_batch(player, tap_count=1, idempotency_key=f"tiny-{number}", at=at)
        assert result.accepted_taps == result.earned_points == 1
    accumulator = BaseTapDailyAccumulator.objects.get()
    assert accumulator.pending_points == 50
    assert BaseTapReceipt.objects.count() == 50
    assert not RewardLedger.objects.exists()
    finalize_tap_accumulator(accumulator.pk, at=at + timedelta(days=1))
    reward = RewardLedger.objects.get(source_type="BASE_TAP")
    assert reward.player_amount == 50 and reward.season == season
    assert reward.config_version == config
    assert PlayerProgression.objects.get(player=player).lifetime_points == 50
    assert PlayerSeason.objects.get(player=player, season=season).season_points == 50
    assert not RewardSettlementOutbox.objects.filter(reward=reward).exists()
    assert not ReferralShareOutcome.objects.filter(source_reward=reward).exists()


@pytest.mark.django_db
def test_rejected_spam_creates_no_receipts_or_pending_scan_growth(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    at = datetime(2026, 1, 2, tzinfo=UTC)
    config = phase4_config_factory(effective_at=at - timedelta(days=1))
    season_factory(config, starts_at=at - timedelta(hours=1))
    player = player_factory()
    accepted = submit_tap_batch(player, tap_count=50, idempotency_key="cap", at=at)
    replay = submit_tap_batch(player, tap_count=50, idempotency_key="cap", at=at)
    assert accepted.created is True and replay.created is False
    for number in range(100):
        result = submit_tap_batch(
            player,
            tap_count=1,
            idempotency_key=f"rejected-{number}",
            at=at + timedelta(seconds=1),
        )
        assert result.accepted_taps == result.earned_points == 0
        assert result.pending_points == 50
    assert BaseTapReceipt.objects.count() == 1
    assert BaseTapDailyAccumulator.objects.get().pending_points == 50


@pytest.mark.django_db
def test_utc_day_boundary_and_idempotency_boundary(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    before = datetime(2026, 1, 2, 23, 59, 59, tzinfo=UTC)
    config = phase4_config_factory(effective_at=before - timedelta(days=1))
    season_factory(
        config, starts_at=before - timedelta(days=1), ends_at=before + timedelta(days=44)
    )
    player = player_factory()
    submit_tap_batch(player, tap_count=10, idempotency_key="boundary", at=before)
    with pytest.raises(ValueError, match="cannot cross"):
        submit_tap_batch(
            player,
            tap_count=10,
            idempotency_key="boundary",
            at=before + timedelta(seconds=1),
        )
    second = submit_tap_batch(
        player,
        tap_count=50,
        idempotency_key="next-day",
        at=before + timedelta(seconds=1),
    )
    assert second.accepted_taps == 50
    assert BaseTapDailyAccumulator.objects.count() == 2


@pytest.mark.django_db
def test_config_boundary_cannot_reuse_receipt(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    at = datetime(2026, 1, 2, 10, tzinfo=UTC)
    first = phase4_config_factory(
        effective_at=at - timedelta(days=1), mutate=no_rate_limit, version="tap-first"
    )
    season_factory(first, starts_at=at - timedelta(days=1))
    player = player_factory()
    submit_tap_batch(player, tap_count=5, idempotency_key="config-key", at=at)
    phase4_config_factory(
        effective_at=at + timedelta(hours=1), mutate=no_rate_limit, version="tap-second"
    )
    with pytest.raises(ValueError, match="cannot cross"):
        submit_tap_batch(
            player,
            tap_count=5,
            idempotency_key="config-key",
            at=at + timedelta(hours=2),
        )


@pytest.mark.django_db
def test_closing_window_rejects_new_taps_without_durable_rows(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    starts = datetime(2026, 1, 1, tzinfo=UTC)
    config = phase4_config_factory(effective_at=starts - timedelta(days=1))
    season_factory(config, starts_at=starts)
    with pytest.raises(ValueError, match="economic window"):
        submit_tap_batch(
            player_factory(),
            tap_count=1,
            idempotency_key="closing",
            at=starts + timedelta(days=42),
        )
    assert not BaseTapDailyAccumulator.objects.exists()
    assert not BaseTapReceipt.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_batches_cannot_exceed_postgres_daily_cap(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    at = datetime(2026, 1, 2, tzinfo=UTC)
    config = phase4_config_factory(effective_at=at - timedelta(days=1), mutate=no_rate_limit)
    season_factory(config, starts_at=at - timedelta(hours=1))
    player = player_factory()

    def submit(key: str) -> int:
        close_old_connections()
        try:
            return submit_tap_batch(player, tap_count=40, idempotency_key=key, at=at).accepted_taps
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(submit, ("concurrent-a", "concurrent-b")))
    assert sum(accepted) == 50
    accumulator = BaseTapDailyAccumulator.objects.get()
    assert accumulator.accepted_taps == 50 and accumulator.pending_points == 50
    assert not RewardLedger.objects.exists()


@pytest.mark.django_db
def test_recovery_continues_after_poison_item(
    monkeypatch: Any,
    settings: Any,
    phase4_config_factory: Any,
    season_factory: Any,
    player_factory: Any,
) -> None:
    settings.BASE_TAP_SETTLEMENT_RETRY_SECONDS = 60
    at = datetime(2026, 1, 2, tzinfo=UTC)
    config = phase4_config_factory(effective_at=at - timedelta(days=1), mutate=no_rate_limit)
    season_factory(config, starts_at=at - timedelta(hours=1))
    poison, healthy = player_factory(801), player_factory(802)
    submit_tap_batch(poison, tap_count=10, idempotency_key="poison", at=at)
    submit_tap_batch(healthy, tap_count=20, idempotency_key="healthy", at=at)
    import apps.base_tap.services as services

    real_settle = getattr(services, "settle_player_reward")  # noqa: B009

    def selectively_fail(**kwargs: Any) -> object:
        if kwargs["player"].pk == poison.pk:
            raise ValueError("poison")
        return real_settle(**kwargs)

    monkeypatch.setattr(services, "settle_player_reward", selectively_fail)
    assert recover_pending_tap_accumulators(at=at + timedelta(days=1)) == 1
    failed = BaseTapDailyAccumulator.objects.get(player=poison)
    settled = BaseTapDailyAccumulator.objects.get(player=healthy)
    assert failed.settlement_state == BaseTapDailyAccumulator.SettlementState.RETRY
    assert failed.last_error_category == "ValueError" and failed.settlement_attempts == 1
    assert settled.settlement_state == BaseTapDailyAccumulator.SettlementState.SETTLED
    assert RewardLedger.objects.filter(player=healthy, source_type="BASE_TAP").count() == 1


@pytest.mark.django_db
def test_historical_recovery_preserves_source_without_regressing_progression(
    phase4_config_factory: Any,
    season_factory: Any,
    player_factory: Any,
    close_season_for_test: Any,
) -> None:
    s1_at = datetime(2026, 1, 2, tzinfo=UTC)
    s1_config = phase4_config_factory(
        effective_at=s1_at - timedelta(days=1), mutate=no_rate_limit, version="s1-config"
    )
    s1 = season_factory(s1_config, starts_at=s1_at - timedelta(hours=1))
    player = player_factory()
    submit_tap_batch(player, tap_count=25, idempotency_key="historical", at=s1_at)
    accumulator = BaseTapDailyAccumulator.objects.get()
    s1 = close_season_for_test(s1)

    s2_at = s1.ends_at + timedelta(days=1)

    def easier_progression(payload: dict[str, Any]) -> None:
        payload["base_tap"]["min_batch_interval_ms"] = 0
        payload["progression"]["levels"][1]["min_lifetime_points"] = 10

    s2_config = phase4_config_factory(
        effective_at=s2_at, mutate=easier_progression, version="s2-config"
    )
    season_factory(s2_config, starts_at=s2_at)
    finalize_tap_accumulator(accumulator.pk, at=s2_at + timedelta(hours=1))
    reward = RewardLedger.objects.get(source_type="BASE_TAP")
    progression = PlayerProgression.objects.get(player=player)
    assert reward.season == s1 and reward.config_version == s1_config
    assert reward.occurred_at == s1_at
    assert progression.config_version == s2_config
    assert progression.level == 2 and progression.dexo_stage == "newborn"


@pytest.mark.django_db
def test_midday_base_tap_policy_publication_rejected_with_open_accumulator(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    at = datetime(2026, 1, 2, 10, tzinfo=UTC)
    config = phase4_config_factory(
        effective_at=at - timedelta(days=1), mutate=no_rate_limit, version="tap-cutover-v1"
    )
    season_factory(config, starts_at=at - timedelta(days=1))
    submit_tap_batch(player_factory(), tap_count=1, idempotency_key="open-day", at=at)

    def change_daily_cap(payload: dict[str, Any]) -> None:
        payload["base_tap"]["rewarded_taps_per_utc_day"] += 1

    with pytest.raises(ValidationError, match="Base Tap policy changes require 00:00 UTC"):
        phase4_config_factory(
            effective_at=at + timedelta(hours=1),
            mutate=change_daily_cap,
            version="tap-cutover-v2",
        )
