"""Synthetic, deterministic policy simulations. No ORM, credentials or network access."""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.arena.policy import CATEGORIES, parse_policy  # noqa: E402
from apps.arena.scoring import (  # noqa: E402
    clan_score,
    core_score,
    creator_scores,
    emission_limit,
    empty_inputs,
    proportional_allocation,
    saturation,
)

POLICY_PATH = (
    Path(__file__).resolve().parents[1] / "config/examples/season1.economic-policy.v1.json"
)
POLICY = parse_policy(json.loads(POLICY_PATH.read_text()))
BUDGETS = dict(
    zip(CATEGORIES, [3_500_000, 2_000_000, 2_000_000, 1_500_000, 1_000_000], strict=True)
)


def population(size: int, active: tuple[str, ...] = CATEGORIES) -> dict[str, Any]:
    carry = dict.fromkeys(CATEGORIES, 0)
    weeks = []
    allocated_total = reserves = 0
    for week in range(1, 7):
        categories: dict[str, Any] = {}
        for category, base in BUDGETS.items():
            scores = dict.fromkeys(range(1, size + 1), 6000) if category in active else {}
            h = size * 7 * 10000 if category == CATEGORIES[2] else 0
            limit, reserve = emission_limit(base, carry[category], category, scores, h, POLICY)
            allocation = proportional_allocation(scores, limit, base // 100)
            amount = sum(allocation.values())
            categories[category] = {
                "fresh": base,
                "carry_in": carry[category],
                "reserve": reserve,
                "supported_limit": limit,
                "allocated": amount,
                "min_player": min(allocation.values(), default=0),
                "max_player": max(allocation.values(), default=0),
            }
            carry[category] = base + carry[category] - amount - reserve
            categories[category]["carry_out"] = carry[category]
            allocated_total += amount
            reserves += reserve
        weeks.append(
            {
                "week": week,
                "categories": categories,
                "allocated": sum(c["allocated"] for c in categories.values()),
            }
        )
    residual = sum(carry.values()) + reserves
    assert allocated_total + residual == 60_000_000
    return {
        "players": size,
        "assumption": (
            "Hypothetical eligible beneficiary count per category; lifecycle-independent upper bound; "
            "Creator H conserves one speaker-day/account/day; no corrections paid"
        ),
        "weeks": weeks,
        "allocated_total": allocated_total,
        "reserves": reserves,
        "final_residual": residual,
    }


def creator_attack(accounts: int, anchored: bool) -> dict[str, int]:
    # Ten coordinated owners repeatedly reuse the same speaker cohort across their groups.
    owners = range(accounts + 1, accounts + 11)
    events: list[dict[str, Any]] = []
    for owner in owners:
        for actor in range(1, accounts + 1):
            for day in range(7):
                for slot in range(4):
                    events.append(
                        {
                            "id": len(events),
                            "player_id": owner,
                            "actor_id": actor,
                            "day": day,
                            "fingerprint": f"opaque:{actor}:{day}:{slot}",
                            "window": day * 100 + slot,
                            "source_seconds": day * 86400 + slot * 2700,
                            "characters": 200,
                        }
                    )
    rows = creator_scores(
        events,
        set(range(1, accounts + 11)),
        set(range(1, accounts + 1)) if anchored else set(),
        POLICY,
    )
    scores = {p: r["score"] for p, r in rows.items() if r["score"] >= 100}
    h = sum(r["h_units"] for r in rows.values())
    limit, _ = emission_limit(2_000_000, 0, CATEGORIES[2], scores, h, POLICY)
    amount = sum(proportional_allocation(scores, limit, 20_000).values())
    assert h <= accounts * 7 * 10000
    return {
        "accounts": accounts,
        "owners": 10,
        "h_units": h,
        "allocated": amount,
        "highest_owner_score": max(scores.values(), default=0),
    }


def examples() -> list[dict[str, Any]]:
    carry = dict.fromkeys(CATEGORIES, 0)
    result = []
    for week in (1, 2):
        raw = {p: empty_inputs(POLICY) for p in range(1, 5)}
        raw[1].update(
            tap=[500] * 7, typing=[[50] * 3 for _ in range(7)], daily_periods=list(range(7))
        )
        raw[2].update(contribution=[115] * 7, meaningful_days=list(range(7)))
        raw[3].update(typing=[[50], [50], [], [], [], [], []], meaningful_days=[0, 1])
        raw[4].update(
            typing=[[50], [50], [], [], [], [], []],
            daily_periods=[0, 1, 2],
            meaningful_days=[0, 1, 2],
            missions=["visit-dalexio:1"] if week == 1 else [],
        )
        # Week-two finalized delayed Clan contribution, never a second Week-one claim score.
        if week == 2:
            raw[4]["contribution"][0] = 40
        scores = {
            CATEGORIES[0]: {
                2: clan_score(raw[2]["contribution"], POLICY),
                **({4: clan_score(raw[4]["contribution"], POLICY)} if week == 2 else {}),
            },
            CATEGORIES[1]: {p: core_score(raw[p], POLICY)[0] for p in (1, 3, 4)},
            CATEGORIES[2]: {},
            CATEGORIES[3]: {3: saturation(1, 5)} if week == 2 else {},
            CATEGORIES[4]: {
                p: 10000 * len(raw[p]["meaningful_days"] if p != 1 else range(7)) // 7 for p in raw
            },
        }
        categories: dict[str, Any] = {}
        for c, base in BUDGETS.items():
            eligible = {p: v for p, v in scores[c].items() if v >= 100}
            limit, reserve = emission_limit(base, carry[c], c, eligible, 0, POLICY)
            allocations = proportional_allocation(eligible, limit, base // 100)
            categories[c] = {
                "scores": eligible,
                "carry_in": carry[c],
                "limit": limit,
                "reserve": reserve,
                "allocations": allocations,
            }
            carry[c] += base - reserve - sum(allocations.values())
            categories[c]["carry_out"] = carry[c]
        result.append(
            {
                "week": week,
                "categories": categories,
                "player_totals": {
                    p: sum(c["allocations"].get(p, 0) for c in categories.values()) for p in raw
                },
            }
        )
    return result


def main() -> None:
    output = {
        "policy": str(POLICY_PATH.name),
        "populations": [population(n) for n in (2, 1000, 10000)],
        "feasible_two_player_core_loyalty": population(2, (CATEGORIES[1], CATEGORIES[4])),
        "adversarial": {
            "all_category_networks": [population(n) for n in (100, 1000)],
            "tap_only_100_and_1000": "0 qualification / 0 allocation",
            "daily_only_100_and_1000": "0 qualification without independent anchor",
            "pending_referrals_100_and_1000": "0 acquisition / 0 global blockers",
            "creator": [creator_attack(n, anchor) for n in (100, 1000) for anchor in (False, True)],
            "residual_risk": (
                "Coordinated accounts that satisfy all authoritative independent routes "
                "can qualify; "
                "no unique-human verification is claimed. See all-category population upper bounds."
            ),
        },
        "four_players": examples(),
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
