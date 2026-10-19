"""Participation eligibility without an Epoch-decision dependency for referral activation."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from apps.players.models import Player


def trusted_observations(players: Sequence[Player]) -> dict[int, datetime]:
    from apps.clans.models import ClanActivity
    from apps.network.models import NetworkActivity
    from apps.telegram.models import TelegramInboundUpdate

    result = {}
    ids = [player.pk for player in players]
    for player in players:
        observed = [v for v in (player.first_authenticated_at, player.first_mini_app_at) if v]
        if observed:
            result[player.pk] = min(observed)
    links = list(
        NetworkActivity.objects.filter(sender_id__in=ids).values_list(
            "sender_id", "telegram_update_id"
        )
    )
    links += list(
        ClanActivity.objects.filter(sender_id__in=ids).values_list(
            "sender_id", "telegram_update_id"
        )
    )
    authenticated = dict(
        TelegramInboundUpdate.objects.filter(
            update_id__in={v for _, v in links}, webhook_authenticated_at__isnull=False
        ).values_list("update_id", "webhook_authenticated_at")
    )
    for player_id, update in links:
        at = authenticated.get(update)
        if at and (player_id not in result or at < result[player_id]):
            result[player_id] = at
    return result


def activity_route(raw: dict[str, Any], *, meaningful_days: int, daily_periods: int) -> bool:
    return len(raw["non_daily_days"]) >= meaningful_days or (
        len(raw["daily_periods"]) >= daily_periods and bool(raw["anchor_days"])
    )


def activation_participation_evidence(
    player: Player, *, season_id: int, at: datetime
) -> dict[str, Any]:
    """Same basic two-day/three-Daily+anchor route; no completed future Epoch needed."""
    from apps.arena.models import EconomicRecognition, PerformanceSourceFact
    from apps.arena.recognition import CORE_FACT_SOURCES
    from apps.clans.models import ClanContributionEntry
    from apps.daily.models import DailyClaim
    from apps.seasons.models import Season

    facts = PerformanceSourceFact.objects.filter(
        player=player, season_id=season_id, source_time__lte=at, source_type__in=CORE_FACT_SOURCES
    ).exclude(source_type__in=["BASE_TAP_BATCH", "BASE_TAP_V2_BATCH"])
    anchors = list(facts.values_list("source_time", flat=True))
    contribution_days = list(
        ClanContributionEntry.objects.filter(
            membership__player=player,
            membership__season_id=season_id,
            occurred_at__lte=at,
            amount__gt=0,
        ).values_list("occurred_at", flat=True)
    )
    daily = list(
        DailyClaim.objects.filter(
            player=player, season_id=season_id, claimed_at__lte=at, reward_ledger__isnull=False
        ).values_list("claim_period", flat=True)
    )
    # Use the same Season-aligned elapsed-day boundaries as weekly qualification.
    start = Season.objects.values_list("starts_at", flat=True).get(pk=season_id)
    observed = trusted_observations([player]).get(player.pk)
    community = list(
        EconomicRecognition.objects.filter(
            epoch__season_id=season_id,
            actor=player,
            component__in=["speaker", "presence"],
            recognized_at__lte=at,
        ).values_list("recognized_at", "source_type", "source_id")
    )
    community_by_day: dict[int, set[tuple[str, str]]] = {}
    for when, source, identity in community:
        community_by_day.setdefault((when - start).days, set()).add((source, identity))
    from apps.arena.models import EpochPolicyBinding
    from apps.arena.policy import parse_policy

    binding = (
        EpochPolicyBinding.objects.select_related("policy")
        .filter(epoch__season_id=season_id)
        .first()
    )
    if binding:
        policy = parse_policy(binding.policy.payload)
        # Bound activation consumes the same proven source receipts as weekly qualification.
        # A backend Mission definition or arbitrary fact cannot independently qualify a referral.
        anchor_receipts = EconomicRecognition.objects.filter(
            epoch__season_id=season_id,
            actor=player,
            player=player,
            component__in=["typing", "mission"],
            recognized_at__lte=at,
        )
        anchors = [
            r.recognized_at
            for r in anchor_receipts
            if r.component == "typing"
            or str(r.inputs.get("definition_identity", "")).rsplit(":", 1)[0]
            in policy.values["independent_mission_keys"]
        ]
        threshold = policy.number("community_messages_per_day")
        meaningful_community = {
            day for day, ids in community_by_day.items() if len(ids) >= threshold
        }
        contribution_by_day: dict[int, int] = {}
        for receipt in EconomicRecognition.objects.filter(
            epoch__season_id=season_id,
            player=player,
            component="contribution",
            recognized_at__lte=at,
        ):
            day = (receipt.recognized_at - start).days
            contribution_by_day[day] = (
                contribution_by_day.get(day, 0) + receipt.inputs["contribution"]
            )
        meaningful_contribution = {
            day
            for day, amount in contribution_by_day.items()
            if amount >= policy.number("clan_meaningful_contribution")
        }
    else:
        meaningful_community = set()
        meaningful_contribution = {(value - start).days for value in contribution_days}
    days = (
        {(value - start).days for value in anchors} | meaningful_community | meaningful_contribution
    )
    return {
        "trusted_observed_at": observed.isoformat() if observed else None,
        "non_daily_days": sorted(days),
        "daily_periods": sorted(set(daily)),
        "anchor_count": len(anchors),
        "fact_ids": list(facts.values_list("pk", flat=True)),
    }


def referral_activation_eligibility(
    player: Player,
    *,
    season_id: int,
    at: datetime,
    min_age_hours: int,
    meaningful_days: int,
    daily_periods: int,
    policy_key: str,
    policy_version: int,
) -> tuple[bool, dict[str, Any]]:
    from apps.arena.models import EconomicReviewDecision, QualificationDecision
    from apps.moderation.services import moderation_state_at

    evidence = activation_participation_evidence(player, season_id=season_id, at=at)
    latest = (
        QualificationDecision.objects.filter(
            player=player,
            effective_at__lte=at,
            policy_key=policy_key,
            policy_version=policy_version,
        )
        .order_by("-effective_at", "-pk")
        .first()
    )
    cleared = bool(
        latest
        and latest.source == "S1_FINALIZED_V1"
        and EconomicReviewDecision.objects.filter(
            epoch_id=latest.evidence.get("epoch_id"),
            player=player,
            outcome="CLEARED",
            created_at__lte=at,
        ).exists()
    )
    if latest and (
        latest.decision == "DISQUALIFIED" or (latest.decision == "PENDING" and not cleared)
    ):
        return False, {**evidence, "blocking_decision_id": latest.pk}
    if moderation_state_at(player, at) in {"BANNED", "SUSPENDED", "FLAGGED"}:
        return False, {**evidence, "reason": "MODERATION_REVIEW"}
    observed = trusted_observations([player]).get(player.pk)
    old_enough = observed is not None and observed <= at - timedelta(hours=min_age_hours)
    route = len(evidence["non_daily_days"]) >= meaningful_days or (
        len(evidence["daily_periods"]) >= daily_periods and evidence["anchor_count"] > 0
    )
    return bool(old_enough and route), evidence
