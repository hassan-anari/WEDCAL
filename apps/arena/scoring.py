"""Reproducible integer scoring from sealed, allowlisted economic recognitions."""

from collections import defaultdict
from datetime import datetime
from fractions import Fraction
from typing import Any

from apps.arena.policy import CATEGORIES, EconomicPolicy


def saturation(value: int, half: int, scale: int = 10000) -> int:
    return scale * value // (value + half)


def core_score(raw: dict[str, Any], policy: EconomicPolicy) -> tuple[int, dict[str, int]]:
    n = policy.number
    days, scale = n("days"), n("scale")
    best = n("typing_runs_per_day")
    quality = sum(sum(sorted(values, reverse=True)[:best]) for values in raw["typing"])
    parts = {
        "tap": n("tap_weight")
        * sum(saturation(v, n("tap_saturation"), scale) for v in raw["tap"])
        // (days * scale),
        "typing": n("typing_weight") * quality // (days * best * n("typing_wpm_ceiling")),
        "daily": n("daily_weight") * min(len(raw["daily_periods"]), days) // days,
        "mission": n("mission_weight")
        * min(len(raw["missions"]), n("mission_limit"))
        // n("mission_limit"),
    }
    return sum(parts.values()), parts


def clan_score(contribution: list[int], policy: EconomicPolicy) -> int:
    n = policy.number
    return n("clan_work_weight") * sum(
        saturation(v, n("clan_saturation"), n("scale")) for v in contribution
    ) // (n("days") * n("scale")) + n("clan_consistency_weight") * sum(
        v >= n("clan_meaningful_contribution") for v in contribution
    ) // n("days")


def creator_scores(
    events: list[dict[str, Any]], qualified: set[int], anchored: set[int], policy: EconomicPolicy
) -> dict[int, dict[str, Any]]:
    n = policy.number
    by_day: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        owner, actor = event["player_id"], event["actor_id"]
        if (
            owner not in qualified
            or actor not in qualified
            or actor not in anchored
            or owner == actor
        ):
            continue
        by_day[(owner, actor, event["day"])].append(event)
    eligible = {}
    choices: dict[tuple[int, int], set[int]] = defaultdict(set)
    for key, items in by_day.items():
        if (
            len({v["fingerprint"] for v in items}) >= n("creator_distinct_messages")
            and len({v["window"] for v in items}) >= n("creator_distinct_messages")
            and max(v["source_seconds"] for v in items) - min(v["source_seconds"] for v in items)
            >= n("creator_speaker_span_seconds")
        ):
            eligible[key] = items
            owner, actor, day = key
            choices[(actor, day)].add(owner)
    result: dict[int, dict[str, Any]] = {}
    for (_, _), owners in choices.items():
        quotient, remainder = divmod(n("scale"), len(owners))
        for index, owner in enumerate(sorted(owners)):
            row = result.setdefault(owner, {"h_units": 0, "windows": [0] * n("days")})
            row["h_units"] += quotient + int(index < remainder)
    unique: dict[tuple[int, int, str], dict[str, Any]] = {}
    for (owner, _, day), items in eligible.items():
        for item in items:
            identity = (owner, day, item["fingerprint"])
            previous = unique.get(identity)
            if previous is None or (item["source_seconds"], item["id"]) < (
                previous["source_seconds"],
                previous["id"],
            ):
                unique[identity] = item
    windows: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for (owner, day, _), item in unique.items():
        windows[(owner, day, item["window"])].append(item)
    for (owner, day, _), items in sorted(windows.items()):
        if len({v["actor_id"] for v in items}) >= n("creator_window_speakers") and sum(
            v["characters"] for v in items
        ) >= n("creator_window_characters"):
            result[owner]["windows"][day] = min(
                result[owner]["windows"][day] + 1, n("creator_windows_per_day")
            )
    for row in result.values():
        h = row["h_units"]
        row["score"] = n("creator_h_weight") * h // (
            h + n("creator_h_saturation") * n("scale")
        ) + n("creator_w_weight") * sum(row["windows"]) // (
            n("days") * n("creator_windows_per_day")
        )
    return result


def empty_inputs(policy: EconomicPolicy) -> dict[str, Any]:
    days = policy.number("days")
    return {
        "tap": [0] * days,
        "typing": [[] for _ in range(days)],
        "daily_periods": [],
        "missions": [],
        "contribution": [0] * days,
        "meaningful_days": [],
        "non_daily_days": [],
        "community_days": [],
        "community": [[] for _ in range(days)],
        "anchor_days": [],
        "referrals": [],
        "sources": [],
        "durable_sources": {},
    }


def aggregate_recognitions(
    records: list[dict[str, Any]], start: datetime, policy: EconomicPolicy
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    people: dict[int, dict[str, Any]] = {}
    creators = []
    n = policy.number
    for record in records:
        player, actor = record["player_id"], record["actor_id"]
        raw = people.setdefault(player, empty_inputs(policy))
        actor_raw = people.setdefault(actor, empty_inputs(policy))
        day = (record["recognized_at"] - start).days
        if not 0 <= day < n("days"):
            raise ValueError("RECOGNITION_OUTSIDE_EPOCH")
        component, inputs = record["component"], record["inputs"]
        raw["sources"].append(f"{record['source_type']}:{record['source_id']}")
        if component == "tap":
            raw["tap"][day] += inputs["accepted_taps"]
        elif component == "typing":
            raw["typing"][day].append(inputs["quality"])
            actor_raw["anchor_days"].append(day)
            actor_raw["meaningful_days"].append(day)
        elif component == "daily":
            raw["daily_periods"].append(inputs["claim_period"])
            actor_raw["meaningful_days"].append(day)
        elif component == "mission":
            raw["missions"].append(inputs["definition_identity"])
            actor_raw["anchor_days"].append(day)
            actor_raw["meaningful_days"].append(day)
        elif component == "contribution":
            raw["contribution"][day] += inputs["contribution"]
        elif component in {"speaker", "presence"}:
            actor_raw["community"][day].append(record["source_id"])
            if component == "speaker":
                creators.append(
                    {
                        **inputs,
                        "player_id": player,
                        "actor_id": actor,
                        "day": day,
                        "id": record["id"],
                    }
                )
        elif component == "acquisition":
            raw["referrals"].append(record["source_id"])
        else:
            raise ValueError("UNAPPROVED_RECOGNITION_COMPONENT")
        if component in {"tap", "typing", "mission"}:
            summary = raw["durable_sources"].setdefault(
                record["source_type"], {"amount": 0, "events": 0}
            )
            summary["amount"] += inputs.get("amount", 0)
            summary["events"] += 1
    for raw in people.values():
        for key in ("daily_periods", "missions", "anchor_days", "meaningful_days", "referrals"):
            raw[key] = sorted(set(raw[key]))
        raw["community_days"] = [
            i
            for i, ids in enumerate(raw["community"])
            if len(set(ids)) >= n("community_messages_per_day")
        ]
        raw["meaningful_days"] = sorted(
            set(raw["meaningful_days"])
            | set(raw["community_days"])
            | {
                i
                for i, value in enumerate(raw["contribution"])
                if value >= n("clan_meaningful_contribution")
            }
        )
        raw["non_daily_days"] = sorted(
            set(raw["anchor_days"])
            | set(raw["community_days"])
            | {
                i
                for i, value in enumerate(raw["contribution"])
                if value >= n("clan_meaningful_contribution")
            }
        )
    return people, creators


def player_scores(
    raw: dict[str, Any], creator: dict[str, Any], policy: EconomicPolicy
) -> dict[str, tuple[int, dict[str, Any]]]:
    n = policy.number
    core, parts = core_score(raw, policy)
    return {
        CATEGORIES[0]: (
            clan_score(raw["contribution"], policy),
            {"contribution_by_day": raw["contribution"]},
        ),
        CATEGORIES[1]: (
            core,
            {
                "components": parts,
                "tap_by_day": raw["tap"],
                "typing_quality_by_day": raw["typing"],
                "daily_periods": raw["daily_periods"],
                "missions": raw["missions"],
                "durable_sources": raw["durable_sources"],
                "durable_source_amount": sum(v["amount"] for v in raw["durable_sources"].values()),
                "durable_source_events": sum(v["events"] for v in raw["durable_sources"].values()),
                "player_amount": 0,
                "reward_event_count": 0,
            },
        ),
        CATEGORIES[2]: (
            creator.get("score", 0),
            creator or {"h_units": 0, "windows": [0] * n("days")},
        ),
        CATEGORIES[3]: (
            saturation(len(raw["referrals"]), n("referral_saturation"), n("scale")),
            {"activated_relations": raw["referrals"]},
        ),
        CATEGORIES[4]: (
            n("scale") * len(raw["meaningful_days"]) // n("days"),
            {"meaningful_days": raw["meaningful_days"]},
        ),
    }


def proportional_allocation(scores: dict[int, int], limit: int, cap: int) -> dict[int, int]:
    pending = {key: value for key, value in scores.items() if value > 0}
    remaining = min(limit, cap * len(pending))
    result: dict[int, int] = {}
    while pending:
        total = sum(pending.values())
        clipped = [key for key, value in pending.items() if remaining * value >= cap * total]
        if clipped:
            for key in clipped:
                result[key] = cap
                remaining -= cap
                del pending[key]
            continue
        for key, value in pending.items():
            result[key] = remaining * value // total
        remainder = remaining - sum(result[key] for key in pending)
        for key in sorted(pending, key=lambda k: (-(remaining * pending[k] % total), k))[
            :remainder
        ]:
            result[key] += 1
        break
    return result


def emission_limit(
    base: int,
    carry: int,
    category: str,
    scores: dict[int, int],
    h_units: int,
    policy: EconomicPolicy,
) -> tuple[int, int]:
    n = policy.number
    reserve = base * n("correction_reserve_bps") // n("scale")
    normal = base - reserve
    participation = Fraction(len(scores))
    if category == CATEGORIES[2]:
        participation = min(participation, Fraction(h_units, n("days") * n("scale")))
    fraction = min(Fraction(1), participation / policy.target(category))
    supported = (normal + min(carry, normal * n("carry_release_bps") // n("scale"))) * fraction
    return min(normal + carry, supported.numerator // supported.denominator), reserve
