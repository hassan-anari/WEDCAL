# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from copy import deepcopy
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.arena.policy import CATEGORIES, parse_policy
from apps.arena.scoring import (
    clan_score,
    core_score,
    creator_scores,
    emission_limit,
    empty_inputs,
    proportional_allocation,
    saturation,
)

from .conftest import POLICY


def test_exact_core_boundaries_and_eight_daily_dates():
    policy = parse_policy(POLICY)
    raw = empty_inputs(policy)
    raw.update(
        tap=[500] * 7, typing=[[50, 50, 50]] * 7, daily_periods=list(range(8)), missions=["a", "b"]
    )
    assert core_score(raw, policy) == (
        5500,
        {"tap": 1250, "typing": 3000, "daily": 1000, "mission": 250},
    )
    assert [saturation(v, 500) for v in [0, 1, 499, 500, 501, 1000, 100000]] == [
        0,
        19,
        4994,
        5000,
        5004,
        6666,
        9950,
    ]
    raw.update(tap=[10**30] * 7, typing=[[100] * 5] * 7, missions=list(range(8)))
    assert core_score(raw, policy)[0] <= 10000


def test_six_epochs_do_not_replay_once_per_version_missions():
    policy = parse_policy(POLICY)

    def sequence(completed):
        scores = []
        for week in range(6):
            raw = empty_inputs(policy)
            raw.update(
                tap=[500] * 7,
                typing=[[50] * 3] * 7,
                daily_periods=list(range(7)),
                missions=list(range(4)) if week == completed else [],
            )
            scores.append(core_score(raw, policy)[0])
        return scores

    assert sequence(0) == [5750, 5250, 5250, 5250, 5250, 5250]
    assert sequence(5) == [5250, 5250, 5250, 5250, 5250, 5750]
    assert sum(sequence(0)) == sum(sequence(5)) == 32000


def test_clan_canonical_booster_and_consistency_vectors():
    policy = parse_policy(POLICY)
    assert clan_score([0] * 7, policy) == 0
    assert clan_score([10] * 7, policy) == 2727
    assert clan_score([40] * 7, policy) == 4285
    assert clan_score([80] * 7, policy) == 5555
    assert clan_score([100] * 7, policy) == 6000
    assert clan_score([100] * 6 + [0], policy) == 5142


def test_exact_waterfill_caps_remainders_and_replay():
    assert proportional_allocation({3: 1, 1: 1, 2: 1}, 5, 10) == {1: 2, 2: 2, 3: 1}
    assert proportional_allocation({1: 10000, 2: 1}, 1000, 100) == {1: 100, 2: 100}
    assert proportional_allocation({}, 1000, 100) == {}


def test_two_player_emission_and_category_specific_carry():
    policy = parse_policy(POLICY)
    limit, reserve = emission_limit(2_000_000, 0, CATEGORIES[1], {1: 1000, 2: 1000}, 0, policy)
    assert (limit, reserve) == (3800, 100000)
    assert proportional_allocation({1: 1000, 2: 1000}, limit, 20000) == {1: 1900, 2: 1900}
    assert emission_limit(2_000_000, 1_900_000, CATEGORIES[2], {1: 6000}, 0, policy) == (0, 100000)
    assert emission_limit(
        2_000_000, 1_900_000, CATEGORIES[1], {i: 100 for i in range(1000)}, 0, policy
    ) == (2_375_000, 100000)


def test_creator_shared_speakers_groups_repetition_and_no_identity_claim():
    policy = parse_policy(POLICY)
    events: list[dict[str, Any]] = []
    for owner in range(10, 20):
        for actor in [1, 2, 3]:
            for day in range(7):
                for slot in range(4):
                    events.append(
                        {
                            "id": len(events),
                            "player_id": owner,
                            "actor_id": actor,
                            "day": day,
                            "fingerprint": f"opaque:{owner}:{actor}:{day}:{slot}",
                            "window": day * 100 + slot,
                            "source_seconds": day * 86400 + slot * 2700,
                            "characters": 200,
                        }
                    )
    result = creator_scores(events, set(range(1, 20)), {1, 2, 3}, policy)
    assert sum(v["h_units"] for v in result.values()) == 210000
    assert all(v["h_units"] == 21000 and v["windows"] == [4] * 7 for v in result.values())
    assert creator_scores(events + events, set(range(1, 20)), {1, 2, 3}, policy) == result
    assert creator_scores(events, set(range(1, 20)), set(), policy) == {}
    assert (
        emission_limit(2_000_000, 0, CATEGORIES[2], {i: 500 for i in range(10)}, 210000, policy)[0]
        == 11400
    )


def test_policy_rejects_unsupported_identity_and_mission_inputs():
    value = deepcopy(POLICY)
    value["independent_mission_keys"] = ["typing-session"]
    with pytest.raises(ValidationError):
        parse_policy(value)
