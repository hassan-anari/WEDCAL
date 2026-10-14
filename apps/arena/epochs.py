from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone

from apps.arena.models import (
    AllocationBatch,
    DlxAllocation,
    EconomicEpoch,
    EpochCategoryBudget,
    EpochPerformanceSnapshot,
    GrowthUnlockEvent,
    PerformanceSourceFact,
)
from apps.arena.qualification import (
    current_qualification_decision,
    is_player_qualified,
    qualified_player_ids_at,
)
from apps.game_config.services import DlxDistributionConfig, resolve_game_config_version
from apps.players.models import Player
from apps.seasons.models import Season


class EpochBlocked(RuntimeError):
    pass


class ScoringPolicyUnconfigured(RuntimeError):
    pass


class SettlementOrderBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DrainBlocker:
    provider: str
    count: int


def initialize_epochs(season: Season) -> list[EconomicEpoch]:
    policy = resolve_game_config_version(season.baseline_config_id).dlx_distribution
    if policy is None:
        raise ValidationError("DLX distribution policy is unavailable")
    per_epoch, remainder = divmod(policy.base_pool_dlx_amount, policy.epoch_count)
    rows = []
    with transaction.atomic():
        for number in range(1, policy.epoch_count + 1):
            start = season.starts_at + timedelta(days=(number - 1) * policy.epoch_days)
            row, _ = EconomicEpoch.objects.get_or_create(
                season=season,
                epoch_number=number,
                defaults={
                    "starts_at": start,
                    "ends_at": start + timedelta(days=policy.epoch_days),
                    "config_version_id": season.baseline_config_id,
                    "base_budget": per_epoch + (1 if number <= remainder else 0),
                },
            )
            rows.append(row)
    return rows


def epoch_for_source_time(season: Season, source_time: datetime) -> EconomicEpoch:
    try:
        return EconomicEpoch.objects.get(
            season=season, starts_at__lte=source_time, ends_at__gt=source_time
        )
    except EconomicEpoch.DoesNotExist as exc:
        raise ValidationError("Source time is outside the six economic epochs") from exc


def drain_blockers(epoch: EconomicEpoch) -> list[DrainBlocker]:
    """Query unfinished durable work scoped to this Season and pre-cutoff source time."""
    from apps.base_tap.models import BaseTapDailyAccumulator
    from apps.clans.models import ClanActivity, ClanActivityWindow
    from apps.network.models import CreatorWindowSettlement, NetworkActivity
    from apps.rewards.models import RewardSettlementOutbox
    from apps.telegram.models import TelegramInboundUpdate
    from apps.typing_game.models import TypingAttempt

    span = {"gte": epoch.starts_at, "lt": epoch.ends_at}
    checks = {
        # Telegram rows have no Season FK; the accepted-at interval is the durable ingress scope.
        "TELEGRAM_INBOUND": TelegramInboundUpdate.objects.filter(
            received_at__gte=epoch.starts_at, received_at__lt=epoch.ends_at
        )
        .exclude(state__in=["PROCESSED", "IGNORED", "DEAD_LETTER"])
        .count(),
        "NETWORK_ACTIVITY": NetworkActivity.objects.filter(
            season=epoch.season,
            observed_at__gte=span["gte"],
            observed_at__lt=span["lt"],
            window_processed=False,
        ).count(),
        "NETWORK_WINDOWS": CreatorWindowSettlement.objects.filter(
            season=epoch.season,
            window_start__gte=span["gte"],
            window_start__lt=span["lt"],
            finalized_at__isnull=True,
        ).count(),
        "CLAN_ACTIVITY": ClanActivity.objects.filter(
            season=epoch.season,
            observed_at__gte=span["gte"],
            observed_at__lt=span["lt"],
            window_processed=False,
        ).count(),
        "CLAN_WINDOWS": ClanActivityWindow.objects.filter(
            season=epoch.season,
            window_start__gte=span["gte"],
            window_start__lt=span["lt"],
            finalized_at__isnull=True,
        ).count(),
        "REWARD_REFERRAL_OUTBOX": RewardSettlementOutbox.objects.filter(
            reward__season=epoch.season,
            reward__occurred_at__gte=span["gte"],
            reward__occurred_at__lt=span["lt"],
        )
        .exclude(state__in=["PROCESSED", "DEFERRED"])
        .count(),
        "BASE_TAP": BaseTapDailyAccumulator.objects.filter(
            season=epoch.season,
            first_accepted_at__gte=span["gte"],
            first_accepted_at__lt=span["lt"],
            pending_points__gt=0,
        )
        .exclude(settlement_state="SETTLED")
        .count(),
        # Unclaimed wallet credit is final user-held state. Only unfinished attempts can change
        # the frozen input.
        "TYPING_ATTEMPTS": TypingAttempt.objects.filter(
            season=epoch.season,
            started_at__gte=span["gte"],
            started_at__lt=span["lt"],
            status=TypingAttempt.Status.STARTED,
        ).count(),
    }
    return [DrainBlocker(key, value) for key, value in checks.items() if value]


def begin_draining(epoch: EconomicEpoch, *, at: datetime) -> EconomicEpoch:
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier

        acquire_economic_write_barrier(epoch.season_id)
        row = EconomicEpoch.objects.select_for_update().get(pk=epoch.pk)
        if row.state == EconomicEpoch.State.OPEN:
            if at < row.ends_at:
                raise EpochBlocked("Epoch has not ended")
            row.state = EconomicEpoch.State.DRAINING
            row.freeze_watermark = {"draining_started_at": at.isoformat(), "providers": "PENDING"}
            row.save(update_fields=("state", "freeze_watermark"))
        return row


def freeze_epoch(epoch: EconomicEpoch, *, at: datetime | None = None) -> EconomicEpoch:
    now = at or timezone.now()
    row = begin_draining(epoch, at=now)
    if row.state in {
        EconomicEpoch.State.FROZEN,
        EconomicEpoch.State.SETTLING,
        EconomicEpoch.State.SETTLED,
    }:
        return row
    blockers = drain_blockers(row)
    if blockers:
        EconomicEpoch.objects.filter(pk=row.pk, state=EconomicEpoch.State.DRAINING).update(
            freeze_watermark={
                "checked_at": now.isoformat(),
                "blockers": {x.provider: x.count for x in blockers},
            }
        )
        raise EpochBlocked(",".join(f"{x.provider}:{x.count}" for x in blockers))
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier

        acquire_economic_write_barrier(row.season_id)
        row = EconomicEpoch.objects.select_for_update().get(pk=row.pk)
        blockers = drain_blockers(row)
        if blockers:
            raise EpochBlocked(",".join(f"{x.provider}:{x.count}" for x in blockers))
        policy = resolve_game_config_version(row.config_version_id).dlx_distribution
        assert policy is not None
        _snapshot_budgets(row, policy)
        _build_performance_snapshots(row, policy)
        row.freeze_watermark = {"checked_at": now.isoformat(), "providers": "CLEAR"}
        row.snapshots_completed_at = now
        row.frozen_at = now
        row.state = EconomicEpoch.State.FROZEN
        row.save(update_fields=("state", "freeze_watermark", "snapshots_completed_at", "frozen_at"))
        return row


def _snapshot_budgets(epoch: EconomicEpoch, policy: DlxDistributionConfig) -> None:
    distributed = 0
    weights = list(policy.category_weights_bps)
    for index, (category, weight) in enumerate(weights):
        base = epoch.base_budget * weight // 10000
        if index == len(weights) - 1:
            base = epoch.base_budget - distributed
        distributed += base
        growth = epoch.growth_budget * weight // 10000
        EpochCategoryBudget.objects.get_or_create(
            epoch=epoch,
            category=category,
            defaults={
                "weight_bps": weight,
                "base_epoch_budget": base,
                "growth_contribution": growth,
                "carry_in": 0,
                "carry_materialized": epoch.epoch_number == 1,
                "effective_budget": base + growth,
                "config_version_id": epoch.config_version_id,
            },
        )


def _build_performance_snapshots(epoch: EconomicEpoch, policy: DlxDistributionConfig) -> None:
    from apps.clans.models import ClanEconomicSettlement
    from apps.rewards.models import RewardLedger

    predicate = policy.qualified_player_predicate
    qualified_ids = qualified_player_ids_at(
        policy_key=str(predicate["policy_key"]),
        policy_version=int(predicate["policy_version"]),
        at=epoch.ends_at,
    )
    decisions = {
        player_id: current_qualification_decision(
            Player(pk=player_id),
            policy_key=str(predicate["policy_key"]),
            policy_version=int(predicate["policy_version"]),
            at=epoch.ends_at,
        )
        for player_id in qualified_ids
    }
    reward_rows = {
        (row["player_id"], row["source_type"]): row
        for row in RewardLedger.objects.filter(
            season=epoch.season,
            player_id__in=qualified_ids,
            occurred_at__gte=epoch.starts_at,
            occurred_at__lt=epoch.ends_at,
        )
        # Typing performance is represented once by its immutable source-time CREDIT fact.
        # A later wallet claim is XP settlement, not a second weekly performance event.
        # Base Tap likewise contributes once through immutable microbatch source-time facts.
        .exclude(source_type__in=("TYPING_CLAIM", "BASE_TAP"))
        # Its passive Referral Share is likewise represented at Typing CREDIT source time.
        .exclude(
            source_type="REFERRAL",
            referral_share_source__source_reward__source_type="TYPING_CLAIM",
        )
        .values("player_id", "source_type")
        .annotate(
            amount=Sum("player_amount"), events=Count("id"), latest_source_at=Max("occurred_at")
        )
    }
    durable_core_rows = list(
        PerformanceSourceFact.objects.filter(
            season=epoch.season,
            player_id__in=qualified_ids,
            source_time__gte=epoch.starts_at,
            source_time__lt=epoch.ends_at,
            category="CORE_GAMEPLAY",
        )
        .values("player_id", "source_type")
        .annotate(amount=Sum("amount"), events=Count("id"), latest_source_at=Max("source_time"))
    )
    durable_core: dict[int, dict[str, object]] = {}
    for source in durable_core_rows:
        player_id = int(source["player_id"])
        aggregate = durable_core.setdefault(
            player_id, {"amount": 0, "events": 0, "by_source_type": {}}
        )
        amount = cast(int, source["amount"])
        events = cast(int, source["events"])
        aggregate["amount"] = cast(int, aggregate["amount"]) + amount
        aggregate["events"] = cast(int, aggregate["events"]) + events
        by_source_type = cast(dict[str, dict[str, int]], aggregate["by_source_type"])
        by_source_type[str(source["source_type"])] = {
            "amount": amount,
            "events": events,
        }
    durable_referral_rows = list(
        PerformanceSourceFact.objects.filter(
            season=epoch.season,
            player_id__in=qualified_ids,
            source_time__gte=epoch.starts_at,
            source_time__lt=epoch.ends_at,
            category="VERIFIED_REFERRAL",
        )
        .values("player_id", "source_type")
        .annotate(amount=Sum("amount"), events=Count("id"), latest_source_at=Max("source_time"))
    )
    durable_referral = {
        int(row["player_id"]): {
            "durable_source_amount": cast(int, row["amount"]),
            "durable_source_events": cast(int, row["events"]),
            "durable_source_type": str(row["source_type"]),
        }
        for row in durable_referral_rows
    }
    clan_rows = {
        row["membership__player_id"]: row
        for row in ClanEconomicSettlement.objects.filter(
            clan_season__season=epoch.season,
            membership__player_id__in=qualified_ids,
            occurred_at__gte=epoch.starts_at,
            occurred_at__lt=epoch.ends_at,
        )
        .values("membership__player_id")
        .annotate(
            gross=Sum("gross_amount"),
            contribution=Sum("contribution_amount"),
            events=Count("id"),
            latest_source_at=Max("occurred_at"),
        )
    }
    for player_id in qualified_ids:
        decision = decisions[player_id]
        assert decision is not None
        all_rewards = [value for (pid, _), value in reward_rows.items() if pid == player_id]
        clan_raw = dict(clan_rows.get(player_id, {}))
        if isinstance(clan_raw.get("latest_source_at"), datetime):
            clan_raw["latest_source_at"] = cast(datetime, clan_raw["latest_source_at"]).isoformat()
        raw_by_category: dict[str, dict[str, object]] = {
            "CLAN_PERFORMANCE": clan_raw,
            "CORE_GAMEPLAY": {
                **_combine_reward_rows(all_rewards),
                "durable_source_amount": (durable_core.get(player_id) or {}).get("amount", 0),
                "durable_source_events": (durable_core.get(player_id) or {}).get("events", 0),
                "durable_sources": (durable_core.get(player_id) or {}).get("by_source_type", {}),
            },
            "CREATOR_COMMUNITY_GROWTH": reward_rows.get((player_id, "CREATOR"), {}),
            "VERIFIED_REFERRAL": {
                **reward_rows.get((player_id, "REFERRAL"), {}),
                **durable_referral.get(player_id, {}),
            },
            "LOYALTY_CONSISTENCY": {
                "reward_event_count": sum(int(row["events"]) for row in all_rewards),
                "source_window_start": epoch.starts_at.isoformat(),
                "source_window_end": epoch.ends_at.isoformat(),
            },
        }
        for category, raw in raw_by_category.items():
            EpochPerformanceSnapshot.objects.create(
                epoch=epoch,
                category=category,
                player_id=player_id,
                raw_inputs=raw,
                performance_units=0,
                qualification_decision=decision,
                config_version_id=epoch.config_version_id,
            )


def _combine_reward_rows(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "player_amount": sum(cast(int, row["amount"]) for row in rows),
        "reward_event_count": sum(cast(int, row["events"]) for row in rows),
        "source_types": sorted(str(row["source_type"]) for row in rows),
    }


def snapshot_player_input(
    *,
    epoch: EconomicEpoch,
    category: str,
    player: Player,
    raw_inputs: dict[str, object],
    performance_units: int,
) -> EpochPerformanceSnapshot:
    row = EconomicEpoch.objects.get(pk=epoch.pk)
    if row.state != EconomicEpoch.State.DRAINING:
        raise ValidationError("Snapshots may only be created by the freeze workflow")
    policy = resolve_game_config_version(row.config_version_id).dlx_distribution
    assert policy is not None
    predicate = policy.qualified_player_predicate
    decision = current_qualification_decision(
        player,
        policy_key=str(predicate["policy_key"]),
        policy_version=int(predicate["policy_version"]),
        at=row.ends_at,
    )
    if not decision or not is_player_qualified(
        player,
        policy_key=str(predicate["policy_key"]),
        policy_version=int(predicate["policy_version"]),
        at=row.ends_at,
    ):
        raise ValidationError("Player has no authoritative qualification at epoch cutoff")
    return EpochPerformanceSnapshot.objects.create(
        epoch=row,
        category=category,
        player=player,
        raw_inputs=raw_inputs,
        performance_units=performance_units,
        qualification_decision=decision,
        config_version_id=row.config_version_id,
    )


AllocationStrategy = Callable[[EpochCategoryBudget, list[EpochPerformanceSnapshot]], dict[int, int]]


def _materialize_carry(row: EconomicEpoch, budget: EpochCategoryBudget) -> None:
    if budget.carry_materialized:
        return
    previous = (
        EconomicEpoch.objects.select_for_update()
        .filter(season=row.season, epoch_number=row.epoch_number - 1)
        .first()
    )
    if previous is None:
        carry = 0
    else:
        if previous.state != EconomicEpoch.State.SETTLED:
            raise SettlementOrderBlocked("Previous epoch carry is not final")
        carry = EpochCategoryBudget.objects.get(epoch=previous, category=budget.category).carry_out
    budget.carry_in = carry
    budget.effective_budget = budget.base_epoch_budget + budget.growth_contribution + carry
    budget.carry_materialized = True
    budget.save(update_fields=("carry_in", "effective_budget", "carry_materialized"))


@transaction.atomic
def settle_epoch(
    epoch: EconomicEpoch,
    *,
    strategies: dict[str, AllocationStrategy] | None = None,
    at: datetime | None = None,
) -> EconomicEpoch:
    from apps.seasons.barrier import acquire_economic_write_barrier

    acquire_economic_write_barrier(epoch.season_id)
    season = Season.objects.select_for_update().get(pk=epoch.season_id)
    row = EconomicEpoch.objects.select_for_update().get(pk=epoch.pk)
    if row.state == EconomicEpoch.State.SETTLED:
        return row
    if row.state != EconomicEpoch.State.FROZEN:
        raise ValidationError("Only a frozen epoch can settle")
    if (
        EconomicEpoch.objects.select_for_update()
        .filter(season=season, epoch_number__lt=row.epoch_number)
        .exclude(state=EconomicEpoch.State.SETTLED)
        .exists()
    ):
        raise SettlementOrderBlocked("Earlier epoch remains unsettled")
    policy = resolve_game_config_version(row.config_version_id).dlx_distribution
    assert policy is not None
    now = at or timezone.now()
    gate_at = season.starts_at + timedelta(
        days=((season.settlement_start_epoch or 1) - 1) * policy.epoch_days
    )
    if now < gate_at:
        raise SettlementOrderBlocked("Settlement operational gate has not opened")
    budgets = list(row.category_budgets.select_for_update().order_by("category"))
    if not strategies or set(b.category for b in budgets) - set(strategies):
        raise ScoringPolicyUnconfigured("SCORING_POLICY_UNCONFIGURED")
    if season.settlement_started_at is None:
        season.settlement_started_at = now
        season.save(update_fields=("settlement_started_at", "updated_at"))
    row.state = EconomicEpoch.State.SETTLING
    row.settlement_started_at = now
    row.save(update_fields=("state", "settlement_started_at"))
    batch, _ = AllocationBatch.objects.get_or_create(
        epoch=row,
        defaults={
            "idempotency_key": f"dlx:epoch:{row.pk}",
            "config_version_id": row.config_version_id,
        },
    )
    for budget in budgets:
        _materialize_carry(row, budget)
        snapshots = list(
            row.performance_snapshots.filter(category=budget.category).order_by("player_id")
        )
        eligible = {snapshot.player_id: snapshot for snapshot in snapshots}
        allocations = strategies[budget.category](budget, snapshots)
        if set(allocations) - set(eligible):
            raise ValidationError("Strategy returned an ineligible beneficiary")
        if any(
            type(key) is not int or type(value) is not int or value < 0
            for key, value in allocations.items()
        ):
            raise ValidationError("Allocation identities and amounts must be nonnegative integers")
        requested_total = sum(allocations.values())
        if requested_total > budget.effective_budget:
            raise ValidationError("Category allocation exceeds budget")
        for player_id, amount in allocations.items():
            if not amount:
                continue
            snapshot = eligible[player_id]
            allocation, created = DlxAllocation.objects.get_or_create(
                batch=batch,
                snapshot=snapshot,
                defaults={
                    "player_id": player_id,
                    "season_id": row.season_id,
                    "epoch": row,
                    "category": budget.category,
                    "amount": amount,
                    "config_version_id": row.config_version_id,
                    "idempotency_key": f"dlx:{batch.pk}:{snapshot.pk}",
                    "metadata": {},
                    "confirmed_at": now,
                },
            )
            if not created and allocation.amount != amount:
                raise ValidationError("Allocation retry conflicts with confirmed history")
        actual = (
            DlxAllocation.objects.filter(
                batch=batch, epoch=row, category=budget.category
            ).aggregate(total=Sum("amount"))["total"]
            or 0
        )
        if actual != requested_total:
            raise ValidationError("Actual allocation ledger does not match strategy result")
        budget.allocated_amount = actual
        budget.carry_out = budget.effective_budget - actual
        budget.residual_amount = 0
        budget.save(update_fields=("allocated_amount", "carry_out", "residual_amount"))
    batch.confirmed_at = now
    batch.save(update_fields=("confirmed_at",))
    row.state = EconomicEpoch.State.SETTLED
    row.settlement_completed_at = now
    row.save(update_fields=("state", "settlement_completed_at"))
    return row


def reconcile_epoch(epoch: EconomicEpoch) -> dict[str, bool]:
    actual = {
        row["category"]: row["total"]
        for row in DlxAllocation.objects.filter(epoch=epoch)
        .values("category")
        .annotate(total=Sum("amount"))
    }
    return {
        budget.category: (
            budget.allocated_amount == actual.get(budget.category, 0)
            and budget.effective_budget
            == actual.get(budget.category, 0) + budget.carry_out + budget.residual_amount
        )
        for budget in epoch.category_budgets.all()
    }


@transaction.atomic
def evaluate_growth_unlocks(season: Season) -> list[GrowthUnlockEvent]:
    from apps.seasons.barrier import acquire_economic_write_barrier

    acquire_economic_write_barrier(season.pk)
    locked = Season.objects.select_for_update().get(pk=season.pk)
    policy = resolve_game_config_version(locked.baseline_config_id).dlx_distribution
    if policy is None or not policy.growth_milestones:
        return []
    predicate = policy.qualified_player_predicate
    qualified = len(
        qualified_player_ids_at(
            policy_key=str(predicate["policy_key"]),
            policy_version=int(predicate["policy_version"]),
            at=timezone.now(),
        )
    )
    for milestone in policy.growth_milestones:
        if qualified >= milestone.qualified_player_count:
            raise ScoringPolicyUnconfigured("GROWTH_DISTRIBUTION_POLICY_UNCONFIGURED")
    return []


def player_dlx_balance(player: Player, season: Season) -> dict[str, int]:
    confirmed = (
        DlxAllocation.objects.filter(player=player, season=season).aggregate(v=Sum("amount"))["v"]
        or 0
    )
    from apps.arena.adjustments import adjustment_totals

    totals = adjustment_totals(player, season)
    return {
        "confirmed": confirmed,
        **totals,
        "net_confirmed": confirmed - totals["forfeited"],
        "claimable": 0,
    }


def reconcile_player_balance(player: Player, season: Season, projection: dict[str, int]) -> bool:
    return player_dlx_balance(player, season) == projection
