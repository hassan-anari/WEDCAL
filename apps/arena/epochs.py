from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.arena.models import (
    AllocationBatch,
    DlxAllocation,
    EconomicEpoch,
    EpochCategoryBudget,
    EpochPerformanceSnapshot,
    GrowthUnlockEvent,
    QualificationDecision,
)
from apps.arena.qualification import current_qualification_decision
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
    """Every provider is durable and filtered by canonical source time before cutoff."""
    from apps.base_tap.models import BaseTapDailyAccumulator
    from apps.clans.models import ClanActivity, ClanActivityWindow
    from apps.network.models import CreatorWindowSettlement, NetworkActivity
    from apps.rewards.models import RewardSettlementOutbox
    from apps.telegram.models import TelegramInboundUpdate
    from apps.typing_game.models import TypingWallet

    checks = {
        "TELEGRAM_INBOUND": TelegramInboundUpdate.objects.filter(received_at__lt=epoch.ends_at)
        .exclude(state__in=["PROCESSED", "IGNORED", "DEAD_LETTER"])
        .count(),
        "NETWORK_ACTIVITY": NetworkActivity.objects.filter(
            observed_at__lt=epoch.ends_at, window_processed=False
        ).count(),
        "NETWORK_WINDOWS": CreatorWindowSettlement.objects.filter(
            window_start__lt=epoch.ends_at, finalized_at__isnull=True
        ).count(),
        "CLAN_ACTIVITY": ClanActivity.objects.filter(
            observed_at__lt=epoch.ends_at, window_processed=False
        ).count(),
        "CLAN_WINDOWS": ClanActivityWindow.objects.filter(
            window_start__lt=epoch.ends_at, finalized_at__isnull=True
        ).count(),
        "REWARD_REFERRAL_OUTBOX": RewardSettlementOutbox.objects.filter(
            reward__occurred_at__lt=epoch.ends_at
        )
        .exclude(state__in=["PROCESSED", "DEFERRED"])
        .count(),
        "BASE_TAP": BaseTapDailyAccumulator.objects.filter(
            first_accepted_at__lt=epoch.ends_at, pending_points__gt=0
        )
        .exclude(settlement_state="SETTLED")
        .count(),
        "TYPING_WALLET": TypingWallet.objects.filter(
            created_at__lt=epoch.ends_at, claimable_balance__gt=0
        ).count(),
    }
    return [DrainBlocker(key, value) for key, value in checks.items() if value]


@transaction.atomic
def freeze_epoch(epoch: EconomicEpoch, *, at: datetime | None = None) -> EconomicEpoch:
    now = at or timezone.now()
    row = EconomicEpoch.objects.select_for_update().get(pk=epoch.pk)
    if row.state in {
        EconomicEpoch.State.FROZEN,
        EconomicEpoch.State.SETTLING,
        EconomicEpoch.State.SETTLED,
    }:
        return row
    if now < row.ends_at:
        raise EpochBlocked("Epoch has not ended")
    row.state = EconomicEpoch.State.DRAINING
    row.save(update_fields=("state",))
    blockers = drain_blockers(row)
    if blockers:
        raise EpochBlocked(",".join(f"{x.provider}:{x.count}" for x in blockers))
    policy = resolve_game_config_version(row.config_version_id).dlx_distribution
    assert policy is not None
    _snapshot_budgets(row, policy)
    row.freeze_watermark = {"checked_at": now.isoformat(), "providers": "CLEAR"}
    row.frozen_at = now
    row.state = EconomicEpoch.State.FROZEN
    row.save(update_fields=("state", "freeze_watermark", "frozen_at"))
    return row


def _snapshot_budgets(epoch: EconomicEpoch, policy: DlxDistributionConfig) -> None:
    previous = (
        EconomicEpoch.objects.filter(season=epoch.season, epoch_number__lt=epoch.epoch_number)
        .order_by("-epoch_number")
        .first()
    )
    distributed = 0
    weights = list(policy.category_weights_bps)
    for index, (category, weight) in enumerate(weights):
        base = epoch.base_budget * weight // 10000
        if index == len(weights) - 1:
            base = epoch.base_budget - distributed
        distributed += base
        carry = 0
        if previous:
            carry = (
                EpochCategoryBudget.objects.filter(epoch=previous, category=category)
                .values_list("carry_out", flat=True)
                .first()
                or 0
            )
        growth = epoch.growth_budget * weight // 10000
        EpochCategoryBudget.objects.get_or_create(
            epoch=epoch,
            category=category,
            defaults={
                "weight_bps": weight,
                "base_epoch_budget": base,
                "growth_contribution": growth,
                "carry_in": carry,
                "effective_budget": base + growth + carry,
                "config_version_id": epoch.config_version_id,
            },
        )


def snapshot_player_input(
    *,
    epoch: EconomicEpoch,
    category: str,
    player: Player,
    raw_inputs: dict[str, object],
    performance_units: int,
) -> EpochPerformanceSnapshot:
    if epoch.state not in {EconomicEpoch.State.DRAINING, EconomicEpoch.State.FROZEN}:
        raise ValidationError("Snapshots may only be fixed during epoch freeze")
    policy = resolve_game_config_version(epoch.config_version_id).dlx_distribution
    assert policy is not None
    predicate = policy.qualified_player_predicate
    decision = current_qualification_decision(
        player,
        policy_key=str(predicate["policy_key"]),
        policy_version=int(predicate["policy_version"]),
        at=epoch.ends_at,
    )
    if not decision or decision.decision != QualificationDecision.Decision.QUALIFIED:
        raise ValidationError("Player has no authoritative qualified decision at epoch cutoff")
    return EpochPerformanceSnapshot.objects.create(
        epoch=epoch,
        category=category,
        player=player,
        raw_inputs=raw_inputs,
        performance_units=performance_units,
        qualification_decision=decision,
        config_version_id=epoch.config_version_id,
    )


AllocationStrategy = Callable[[EpochCategoryBudget, list[EpochPerformanceSnapshot]], dict[int, int]]


@transaction.atomic
def settle_epoch(
    epoch: EconomicEpoch,
    *,
    strategies: dict[str, AllocationStrategy] | None = None,
    at: datetime | None = None,
) -> EconomicEpoch:
    row = EconomicEpoch.objects.select_for_update().select_related("season").get(pk=epoch.pk)
    if row.state == EconomicEpoch.State.SETTLED:
        return row
    if row.state != EconomicEpoch.State.FROZEN:
        raise ValidationError("Only a frozen epoch can settle")
    earlier = (
        EconomicEpoch.objects.select_for_update()
        .filter(season=row.season, epoch_number__lt=row.epoch_number)
        .exclude(state=EconomicEpoch.State.SETTLED)
    )
    if earlier.exists():
        raise SettlementOrderBlocked("Earlier epoch remains unsettled")
    if row.epoch_number < (row.season.settlement_start_epoch or 1):
        raise SettlementOrderBlocked("Settlement operational gate has not opened")
    if not strategies:
        raise ScoringPolicyUnconfigured("SCORING_POLICY_UNCONFIGURED")
    missing = set(row.category_budgets.values_list("category", flat=True)) - set(strategies)
    if missing:
        raise ScoringPolicyUnconfigured("SCORING_POLICY_UNCONFIGURED")
    row.state = EconomicEpoch.State.SETTLING
    row.settlement_started_at = at or timezone.now()
    row.save(update_fields=("state", "settlement_started_at"))
    if row.season.settlement_started_at is None:
        row.season.settlement_started_at = row.settlement_started_at
        row.season.save(update_fields=("settlement_started_at", "updated_at"))
    batch, _ = AllocationBatch.objects.get_or_create(
        epoch=row,
        defaults={
            "idempotency_key": f"dlx:epoch:{row.pk}",
            "config_version_id": row.config_version_id,
        },
    )
    for budget in row.category_budgets.select_for_update().order_by("category"):
        snapshots = list(
            row.performance_snapshots.filter(category=budget.category).order_by("player_id")
        )
        allocations = strategies[budget.category](budget, snapshots)
        if any(type(v) is not int or v < 0 for v in allocations.values()):
            raise ValidationError("Allocation amounts must be nonnegative integers")
        total = sum(allocations.values())
        if total > budget.effective_budget:
            raise ValidationError("Category allocation exceeds budget")
        for snapshot in snapshots:
            amount = allocations.get(snapshot.player_id, 0)
            if amount:
                DlxAllocation.objects.get_or_create(
                    batch=batch,
                    snapshot=snapshot,
                    defaults={
                        "player_id": snapshot.player_id,
                        "season_id": row.season_id,
                        "epoch": row,
                        "category": budget.category,
                        "amount": amount,
                        "config_version_id": row.config_version_id,
                        "idempotency_key": f"dlx:{batch.pk}:{snapshot.pk}",
                        "metadata": {},
                        "confirmed_at": at or timezone.now(),
                    },
                )
        budget.allocated_amount = total
        budget.carry_out = budget.effective_budget - total
        budget.residual_amount = 0
        budget.save(update_fields=("allocated_amount", "carry_out", "residual_amount"))
    batch.confirmed_at = at or timezone.now()
    batch.save(update_fields=("confirmed_at",))
    row.state = EconomicEpoch.State.SETTLED
    row.settlement_completed_at = batch.confirmed_at
    row.save(update_fields=("state", "settlement_completed_at"))
    return row


def reconcile_epoch(epoch: EconomicEpoch) -> dict[str, bool]:
    return {
        budget.category: budget.effective_budget
        == budget.allocated_amount + budget.carry_out + budget.residual_amount
        for budget in epoch.category_budgets.all()
    }


@transaction.atomic
def evaluate_growth_unlocks(season: Season) -> list[GrowthUnlockEvent]:
    locked = Season.objects.select_for_update().get(pk=season.pk)
    policy = resolve_game_config_version(locked.baseline_config_id).dlx_distribution
    if policy is None or not policy.growth_milestones:
        return []
    predicate = policy.qualified_player_predicate
    qualified = (
        QualificationDecision.objects.filter(
            policy_key=predicate["policy_key"],
            policy_version=predicate["policy_version"],
            decision="QUALIFIED",
        )
        .values("player_id")
        .distinct()
        .count()
    )
    created: list[GrowthUnlockEvent] = []
    for milestone in policy.growth_milestones:
        if qualified < milestone.qualified_player_count:
            continue
        # OPEN-epoch distribution remains fail-closed until an approved policy exists.
        raise ScoringPolicyUnconfigured("GROWTH_DISTRIBUTION_POLICY_UNCONFIGURED")
    return created


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
