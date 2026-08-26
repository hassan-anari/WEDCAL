import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import publish_game_config
from apps.players.models import Player
from apps.seasons.models import Season
from apps.seasons.services import activate_season

BASE_CONFIG = json.loads(
    (
        Path(__file__).resolve().parents[1] / "config/examples/season1.game-config.v1.json"
    ).read_text()
)


@pytest.fixture
def phase4_config_factory(db) -> Callable[..., GameConfigVersion]:  # type: ignore[no-untyped-def]
    sequence = 0

    def create(
        *,
        effective_at: datetime | None = None,
        mutate: Callable[[dict[str, Any]], None] | None = None,
        version: str | None = None,
    ) -> GameConfigVersion:
        nonlocal sequence
        sequence += 1
        payload = copy.deepcopy(BASE_CONFIG)
        if mutate:
            mutate(payload)
        effective = effective_at or datetime(2026, 1, 1, tzinfo=UTC)
        config = GameConfigVersion.objects.create(
            version=version or f"phase4-v{sequence}", effective_at=effective, payload=payload
        )
        return publish_game_config(config, published_at=effective - timedelta(seconds=1))

    return create


@pytest.fixture
def player_factory(db) -> Callable[[int], Player]:  # type: ignore[no-untyped-def]
    def create(telegram_id: int = 100) -> Player:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return Player.objects.create(
            telegram_user_id=telegram_id,
            first_name="Dexo",
            first_seen_at=now,
            last_seen_at=now,
            first_seen_source=Player.FirstSeenSource.MINI_APP,
        )

    return create


@pytest.fixture
def season_factory(db) -> Callable[..., Season]:  # type: ignore[no-untyped-def]
    sequence = 0

    def create(
        config: GameConfigVersion,
        *,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> Season:
        nonlocal sequence
        sequence += 1
        start = starts_at or datetime(2026, 1, 1, tzinfo=UTC)
        season = Season.objects.create(
            code=f"phase4-s{sequence}",
            title="Phase 4",
            starts_at=start,
            ends_at=ends_at or start + timedelta(days=90),
            baseline_config=config,
        )
        return activate_season(season)

    return create
