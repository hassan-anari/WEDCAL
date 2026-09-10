import copy
import json
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError

from apps.game_config.services import parse_economy_sync

CANONICAL = Path(__file__).resolve().parents[3] / "config/examples/season1.game-config.v1.json"


def payload() -> dict[str, object]:
    return json.loads(CANONICAL.read_text())  # type: ignore[no-any-return]


def test_economy_sync_canonical_config_is_typed() -> None:
    tap, distribution = parse_economy_sync(payload())
    assert tap.points_per_rewarded_tap == 1
    assert tap.rewarded_taps_per_day == 50
    assert distribution.epoch_count == 6
    assert sum(value for _, value in distribution.category_weights_bps) == 10000
    assert distribution.growth_milestones == ()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["dlx_distribution"]["timing"].update(epoch_count=7),
        lambda value: value["dlx_distribution"].update(default_settlement_start_epoch=7),
        lambda value: value["dlx_distribution"]["pool"].update(max_dlx_amount=1),
        lambda value: value["dlx_distribution"]["category_weights_bps"].update(CORE_GAMEPLAY=1),
        lambda value: value["base_tap"].update(points_per_rewarded_tap=0),
        lambda value: value["base_tap"].update(max_taps_per_batch=51),
    ],
)
def test_economy_sync_rejects_invalid_values(mutation: object) -> None:
    value = copy.deepcopy(payload())
    mutation(value)  # type: ignore[operator]
    with pytest.raises(ValidationError):
        parse_economy_sync(value)


def test_growth_milestones_must_be_unique_and_fit_pool() -> None:
    value = payload()
    growth = value["dlx_distribution"]["pool"]["growth_unlock"]  # type: ignore[index]
    growth["milestones"] = [
        {"key": "same", "qualified_player_count": 100, "unlock_dlx_amount": 30_000_000},
        {"key": "same", "qualified_player_count": 200, "unlock_dlx_amount": 20_000_000},
    ]
    with pytest.raises(ValidationError):
        parse_economy_sync(value)
