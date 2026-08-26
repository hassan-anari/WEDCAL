import json
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError

from apps.game_config.services import parse_game_config

CANONICAL = Path(__file__).resolve().parents[3] / "config/examples/season1.game-config.v1.json"


def test_canonical_season_one_config_passes_phase4_validation() -> None:
    progression, daily, missions, typing = parse_game_config(json.loads(CANONICAL.read_text()))
    assert progression.levels[0].level == 1
    assert daily.reward_cycle_points == (80, 90, 100, 110, 120, 140, 180)
    assert missions.definition("typing-session").repeat == "DAILY"
    assert typing.max_reward_per_attempt == 90


def test_phase4_config_rejects_float_economy_values() -> None:
    payload = json.loads(CANONICAL.read_text())
    payload["typing"]["attempts_per_day"] = 5.0
    with pytest.raises(ValidationError):
        parse_game_config(payload)
