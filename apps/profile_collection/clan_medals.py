"""Historical Clan evidence for zero-point career medals.

Owner-approved loyalty semantics: cumulative complete 24-hour ACTIVE membership
time, in one Clan and Season; access-loss gaps excluded, overlapping time unioned.
Missing activation history never falls back to a current role or projection.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from apps.clans.models import ClanHQXPEntry, ClanMembership, ClanSeason
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ResolvedGameConfig, resolve_game_config_version
from apps.players.models import Player

Interval = tuple[datetime, datetime]


def active_intervals(member: ClanMembership, at: datetime) -> list[Interval]:
    cs = member.clan_season
    start = max(member.started_at, cs.season.starts_at, cs.created_at)
    end = min(at, member.ended_at or at, cs.season.ends_at)
    for clan_event in cs.events.all():
        if clan_event.kind in {"CLAN_SEASON_DISSOLVED", "SEASON_CLOSE_ENDED"}:
            end = min(end, clan_event.occurred_at)
    intervals: list[Interval] = []
    opened = None
    for event in sorted(member.events.all(), key=lambda e: (e.occurred_at, e.pk)):
        if (
            event.kind not in {"ACTIVATED", "ACCESS_LOST", "ACCESS_RESTORED"}
            or event.occurred_at > end
        ):
            continue
        if event.kind in {"ACTIVATED", "ACCESS_RESTORED"}:
            if opened is None:
                opened = max(start, event.occurred_at)
        elif opened is not None:
            if event.occurred_at > opened:
                intervals.append((opened, event.occurred_at))
            opened = None
    if opened is not None and member.access_state == ClanMembership.Access.ACCESS_LOST:
        end = min(end, member.access_lost_at or opened)
    if opened is not None and end > opened:
        intervals.append((opened, end))
    return intervals


def merged_intervals(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _leadership_evidence(
    cs: ClanSeason, members: list[ClanMembership], at: datetime, level: int
) -> dict[str, Any] | None:
    # Include the evaluation instant for an ongoing qualifying Captain interval.
    # Restoration events allow late reconciliation without fabricating an HQ event.
    # The extra microsecond includes the as-of instant, while persisted membership,
    # dissolution and Season ends remain strictly exclusive.
    intervals = merged_intervals(
        [i for m in members for i in active_intervals(m, at + timedelta(microseconds=1))]
    )
    events = sorted(
        (e for e in cs.events.all() if e.occurred_at <= at),
        key=lambda e: (e.occurred_at, e.pk),
    )
    entries = list(
        cs.hq_xp_entries.filter(
            occurred_at__lte=at,
            occurred_at__gte=cs.season.starts_at,
            occurred_at__lt=cs.season.ends_at,
        ).order_by("occurred_at", "pk")
    )
    candidates = {e.occurred_at for e in entries}
    candidates.update(start for start, _ in intervals)
    candidates.update(e.occurred_at for e in events)
    candidates.add(at)
    total = 0
    position = 0
    event_position = 0
    authority = bot = True
    recovery_causes: set[str] = set()
    targets: dict[int, int | None] = {}
    last: ClanHQXPEntry | None = None
    for candidate in sorted(candidates):
        while event_position < len(events) and events[event_position].occurred_at <= candidate:
            event = events[event_position]
            if event.kind in {"CAPTAIN_AUTHORITY_LOST", "CAPTAIN_AUTHORITY_RESTORED"}:
                authority = event.kind == "CAPTAIN_AUTHORITY_RESTORED"
                if authority:
                    recovery_causes.discard("CAPTAIN_AUTHORITY")
            elif event.kind in {"BOT_ADMIN_LOST", "BOT_ADMIN_RESTORED"}:
                bot = event.kind == "BOT_ADMIN_RESTORED"
                if bot:
                    recovery_causes.discard("BOT_ADMIN")
            elif event.kind == "CAPTAIN_INACTIVITY_RECOVERED":
                recovery_causes.discard("INACTIVITY")
            elif (
                event.kind == "GOVERNANCE_OPENED"
                and event.metadata.get("case_type") == "RECOVERY_DISSOLUTION"
            ):
                recovery_causes.add("GOVERNANCE")
            elif event.kind == "RECOVERY_ENTERED":
                recovery_causes.add(event.source_type or "UNKNOWN")
            elif event.kind == "RECOVERY_CANCELLED":
                recovery_causes.clear()
            event_position += 1
        while position < len(entries) and entries[position].occurred_at <= candidate:
            last = entries[position]
            total += last.amount
            position += 1
        if last is None:
            continue
        if last.config_version_id not in targets:
            policy = resolve_game_config_version(last.config_version_id).clan
            targets[last.config_version_id] = (
                next((r.min_xp for r in policy.hq_curve if r.level == level), None)
                if policy
                else None
            )
        target = targets[last.config_version_id]
        if target is None or total < target:
            continue
        if (
            not any(left <= candidate < right for left, right in intervals)
            or not authority
            or not bot
            or recovery_causes
        ):
            continue
        return {
            "clan_season_id": cs.pk,
            "season_id": cs.season_id,
            "captain_membership_ids": [m.pk for m in members],
            "qualified_at": candidate.isoformat(),
            "hq_xp": total,
            "hq_level": level,
            "hq_threshold": target,
            "last_hq_entry_id": last.pk,
            "hq_config_version_id": last.config_version_id,
        }
    return None


def qualifying_clan_medals(
    player: Player, resolved: ResolvedGameConfig, at: datetime, *, keys: set[str]
) -> dict[str, dict[str, Any]]:
    assert resolved.season_honors is not None
    thresholds = {r.key: r.threshold for r in resolved.season_honors.medals}
    by_clan: dict[int, list[ClanMembership]] = defaultdict(list)
    for member in (
        ClanMembership.objects.filter(player=player, started_at__lte=at)
        .select_related("clan_season__season")
        .prefetch_related("events", "clan_season__events")
        .order_by("clan_season_id", "started_at", "pk")
    ):
        # Corrupt cross-Season pointers cannot establish an entitlement.
        if member.season_id == member.clan_season.season_id:
            by_clan[member.clan_season_id].append(member)
    if not by_clan:
        return {}
    result: dict[str, dict[str, Any]] = {}
    rule = (
        GameConfigVersion.objects.get(pk=resolved.id)
        .payload["season_honors"]["medals"]["leadership"]
        .get("rule")
        if "leadership" in keys
        else None
    )
    for members in by_clan.values():
        cs = members[0].clan_season
        if "clan-loyalty" in keys and "clan-loyalty" not in result:
            intervals = merged_intervals([i for m in members for i in active_intervals(m, at)])
            duration = sum((end - start for start, end in intervals), timedelta())
            days = thresholds["clan-loyalty"]
            assert days is not None
            if duration >= timedelta(days=days):
                result["clan-loyalty"] = {
                    "semantics": "CUMULATIVE_ACTIVE_ELAPSED_V1",
                    "clan_season_id": cs.pk,
                    "season_id": cs.season_id,
                    "membership_ids": [m.pk for m in members],
                    "threshold_days": days,
                    "active_microseconds": duration // timedelta(microseconds=1),
                    "intervals": [[start.isoformat(), end.isoformat()] for start, end in intervals],
                }
        if "leadership" in keys and "leadership" not in result and cs.captain_id == player.pk:
            if rule != "CAPTAIN_AND_CLAN_HQ_LEVEL_5":
                continue  # Unsupported policy cannot silently award.
            captain_members = [m for m in members if m.role == "CAPTAIN"]
            evidence = _leadership_evidence(
                cs, captain_members, at, int(rule.removeprefix("CAPTAIN_AND_CLAN_HQ_LEVEL_"))
            )
            if evidence:
                result["leadership"] = evidence
    return result
