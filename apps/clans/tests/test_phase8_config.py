import copy

import pytest
from django.core.exceptions import ValidationError

from apps.conftest import BASE_CONFIG
from apps.game_config.services import parse_clan


def test_canonical_phase8_clan_config_parses() -> None:
    policy = parse_clan(copy.deepcopy(BASE_CONFIG))
    assert policy.activity.context == "CLAN_HOME"
    assert policy.max_capacity_including_items == 65
    assert {x.item_id for x in policy.inventory_items if x.available_s1} == {
        "rally-beacon",
        "contribution-booster",
        "mission-progress-booster",
        "coordination-booster",
        "capacity-expansion-module",
        "roster-slot-module",
        "command-relay",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["clan"]["treasury"].update(split_bps=10000),
        lambda p: p["clan"]["captain_bonus"].update(share_bps=10001),
        lambda p: p["anti_fraud"]["clan"].update(same_fingerprint_suspicious_count=4),
        lambda p: p["clan"]["capacity"]["hq_curve"][1].update(min_cumulative_hq_xp=0),
        lambda p: p["clan"]["capacity"].update(max_capacity_including_items=10),
        lambda p: p["clan"]["inventory"]["items"][0]["effects"][0].update(value=10001),
        lambda p: p["clan"]["inventory"]["items"][0].update(activation="CONTEXT"),
        lambda p: p["clan"]["contribution"].update(gross_points_to_contribution_denominator=1.5),
    ],
)
def test_invalid_phase8_config_is_rejected(mutate) -> None:  # type: ignore[no-untyped-def]
    payload = copy.deepcopy(BASE_CONFIG)
    mutate(payload)
    with pytest.raises(ValidationError):
        parse_clan(payload)


def test_network_and_clan_fraud_configs_are_independent() -> None:
    payload = copy.deepcopy(BASE_CONFIG)
    payload["anti_fraud"]["clan"]["same_fingerprint_suspicious_count"] = 3
    assert parse_clan(payload).anti_fraud.suspicious_count == 3
    assert payload["anti_fraud"]["network"]["same_fingerprint_suspicious_count"] == 2
