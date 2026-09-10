from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.seasons.models import SeasonSettlementControlEvent
from apps.seasons.services import set_settlement_start_epoch


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
