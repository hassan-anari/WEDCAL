from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.daily.models import DailyClaim
from apps.daily.services import claim_daily
from apps.seasons.models import Season, SeasonSettlementControlEvent
from apps.seasons.services import (
    OutsideEconomicWindow,
    activate_season,
    require_reward_earning_time,
    set_settlement_start_epoch,
)
from apps.typing_game.models import TypingAttempt
from apps.typing_game.services import start_attempt


@pytest.mark.django_db
def test_settlement_start_is_audited_and_freezes_after_start(
    phase4_config_factory: Any, season_factory: Any
) -> None:
    at = datetime(2026, 1, 2, tzinfo=UTC)
    config = phase4_config_factory(effective_at=at - timedelta(days=1))
    season = season_factory(config, starts_at=at - timedelta(hours=1))
    set_settlement_start_epoch(season, 2)
    event = SeasonSettlementControlEvent.objects.get(season=season)
    assert (event.previous_start_epoch, event.new_start_epoch) == (3, 2)
    season.refresh_from_db()
    season.settlement_started_at = at
    season.save(update_fields=("settlement_started_at",))
    with pytest.raises(ValidationError):
        set_settlement_start_epoch(season, 4)


@pytest.mark.django_db
def test_schema_v2_activation_requires_canonical_timing_and_uses_config_default(
    phase4_config_factory: Any,
) -> None:
    starts = datetime(2026, 1, 1, tzinfo=UTC)
    config = phase4_config_factory(effective_at=starts - timedelta(days=1))
    invalid = Season.objects.create(
        code="invalid-timing",
        title="Invalid",
        starts_at=starts,
        ends_at=starts + timedelta(days=44),
        baseline_config=config,
    )
    with pytest.raises(ValidationError, match="product period"):
        activate_season(invalid)
    valid = Season.objects.create(
        code="valid-timing",
        title="Valid",
        starts_at=starts,
        ends_at=starts + timedelta(days=45),
        baseline_config=config,
    )
    activate_season(valid)
    valid.refresh_from_db()
    assert (
        valid.settlement_start_epoch
        == config.payload["dlx_distribution"]["default_settlement_start_epoch"]
    )


@pytest.mark.django_db
def test_day_42_is_economic_and_closing_window_is_not(
    phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    starts = datetime(2026, 1, 1, tzinfo=UTC)
    config = phase4_config_factory(effective_at=starts - timedelta(days=1))
    season = season_factory(config, starts_at=starts)
    require_reward_earning_time(season, starts + timedelta(days=42) - timedelta(microseconds=1))
    claim_daily(player_factory(701), starts + timedelta(days=42) - timedelta(microseconds=1))
    with pytest.raises(OutsideEconomicWindow):
        require_reward_earning_time(season, starts + timedelta(days=42))
    with pytest.raises(OutsideEconomicWindow):
        claim_daily(player_factory(702), starts + timedelta(days=42))
    with pytest.raises(OutsideEconomicWindow):
        start_attempt(player_factory(703), "closing-typing", starts + timedelta(days=42))
    assert DailyClaim.objects.count() == 1
    assert not TypingAttempt.objects.exists()
