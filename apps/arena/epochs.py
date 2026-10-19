from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Count, Q, Sum
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
    from apps.arena.recognition import bound_policy

    if bound_policy(epoch) is not None:
        from apps.arena.economics import scoped_work

        return [DrainBlocker(reason, 1) for reason in scoped_work(epoch)]
    from apps.base_tap.models import BaseTapDailyAccumulator
    from apps.clans.models import ClanActivity, ClanActivityWindow, ClanMissionSignal
    from apps.missions.models import PlayerMission
    from apps.network.models import CreatorWindowSettlement, NetworkActivity
    from apps.rewards.models import RewardSettlementOutbox
    from apps.telegram.models import TelegramInboundUpdate
    from apps.typing_game.models import TypingAttempt

    span = {"gte": epoch.starts_at, "lt": epoch.ends_at}
    checks = {
        # Ingress has no authoritative economic source-time column. Unknown impact remains
        # a conservative global blocker, including dead letters and late received updates.
        "TELEGRAM_INBOUND": TelegramInboundUpdate.objects.all()
        .exclude(state__in=["PROCESSED", "IGNORED"])
        .count(),
        "NETWORK_ACTIVITY": NetworkActivity.objects.filter(
            season=epoch.season,
            observed_at__lt=span["lt"],
            window_processed=False,
        ).count(),
        "NETWORK_WINDOWS": CreatorWindowSettlement.objects.filter(
            season=epoch.season,
            window_start__lt=span["lt"],
            finalized_at__isnull=True,
        ).count(),
        "CLAN_ACTIVITY": ClanActivity.objects.filter(
            season=epoch.season,
            observed_at__lt=span["lt"],
            window_processed=False,
        ).count(),
        "CLAN_WINDOWS": ClanActivityWindow.objects.filter(
            clan_season__season=epoch.season,
            window_start__lt=span["lt"],
            finalized_at__isnull=True,
        ).count(),
        "CLAN_MISSION_SIGNALS": ClanMissionSignal.objects.filter(
            membership__season=epoch.season,
            occurred_at__lt=span["lt"],
            processed_at__isnull=True,
        ).count(),
        "REWARD_REFERRAL_OUTBOX": RewardSettlementOutbox.objects.filter(
            reward__season=epoch.season,
            reward__occurred_at__lt=span["lt"],
        )
        .exclude(state="PROCESSED")
        .count(),
        "BASE_TAP": BaseTapDailyAccumulator.objects.filter(
            season=epoch.season,
            pending_points__gt=0,
        )
        .exclude(settlement_state="SETTLED")
        .filter(Q(first_accepted_at__isnull=True) | Q(first_accepted_at__lt=span["lt"]))
        .count(),
        # Unclaimed wallet credit is final user-held state. Only unfinished attempts can change
        # the frozen input.
        "MISSION_ENTITLEMENTS": PlayerMission.objects.filter(
            season=epoch.season,
            status="REWARD_READY",
        )
        .filter(Q(verified_at__isnull=True) | Q(verified_at__lt=span["lt"]))
        .count(),
        "TYPING_ATTEMPTS": TypingAttempt.objects.filter(
            season=epoch.season,
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
    from apps.arena.readiness import readiness_blockers
    from apps.seasons.barrier import acquire_close_barrier

    blocked: list[str] = []
    with transaction.atomic():
        # Exclusive form of the existing Season barrier, before any model row locks.
        # No preflight or earlier evaluation is trusted across this transaction boundary.
        acquire_close_barrier(row.season_id)
        row = EconomicEpoch.objects.select_for_update().get(pk=row.pk)
        if row.state in {"FROZEN", "SETTLING", "SETTLED"}:
            return row
        blocked, evaluation = readiness_blockers(row)
        # An unconfigured/incomplete evaluation already prevents Freeze. Avoid scanning global
        # ingress/source tables while holding the exclusive writer barrier in that common state.
        # The read-only preflight still reports all source blockers without excluding writers.
        if not blocked:
            blocked.extend(f"DRAIN_{x.provider}:{x.count}" for x in drain_blockers(row))
        if blocked:
            row.freeze_watermark = {"checked_at": now.isoformat(), "blockers": sorted(set(blocked))}
            row.save(update_fields=("freeze_watermark",))
        else:
            assert evaluation is not None
            policy = resolve_game_config_version(row.config_version_id).dlx_distribution
            assert policy is not None
            _snapshot_budgets(row, policy)
            _build_performance_snapshots(row, policy)
            row.freeze_watermark = {
                "checked_at": now.isoformat(),
                "providers": "CLEAR",
                "readiness_evaluation_id": evaluation.pk,
                "input_digest": evaluation.input_digest,
            }
            row.snapshots_completed_at = now
            row.frozen_at = now
            row.state = EconomicEpoch.State.FROZEN
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('dexo.epoch_freeze', 'on', true)")
            row.save(
                update_fields=("state", "freeze_watermark", "snapshots_completed_at", "frozen_at")
            )
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('dexo.epoch_freeze', 'off', true)")
    if blocked:
        raise EpochBlocked(",".join(sorted(set(blocked))))
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
        budget, _ = EpochCategoryBudget.objects.get_or_create(
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
        from apps.arena.models import CategoryCorrectionReserve
        from apps.arena.recognition import bound_policy

        selected = bound_policy(epoch)
        if selected is not None:
            binding, economic_policy = selected
            if growth:
                raise ValidationError("S1_GROWTH_MILESTONES_EMPTY")
            CategoryCorrectionReserve.objects.get_or_create(
                budget=budget,
                defaults={
                    "amount": base
                    * economic_policy.number("correction_reserve_bps")
                    // economic_policy.number("scale"),
                    "policy": binding.policy,
                },
            )


def _build_performance_snapshots(epoch: EconomicEpoch, policy: DlxDistributionConfig) -> None:
    from apps.arena.recognition import CORE_FACT_SOURCES, bound_policy

    if bound_policy(epoch) is not None:
        from apps.arena.economics import build_finalized_snapshots

        build_finalized_snapshots(epoch)
        return
    # Unbound history retains zero-score diagnostics and the Phase 1 fail-closed contract.
    # No RewardLedger amount, passive share, claimed reward or derived Clan projection is Core.
    from apps.clans.models import ClanContributionEntry
    from apps.daily.models import DailyClaim

    predicate = policy.qualified_player_predicate
    qualified_ids = qualified_player_ids_at(
        policy_key=str(predicate["policy_key"]),
        policy_version=int(predicate["policy_version"]),
        at=epoch.ends_at,
    )
    for player_id in qualified_ids:
        decision = current_qualification_decision(
            Player(pk=player_id),
            policy_key=str(predicate["policy_key"]),
            policy_version=int(predicate["policy_version"]),
            at=epoch.ends_at,
        )
        assert decision is not None
        sources = {}
        facts = PerformanceSourceFact.objects.filter(
            season_id=epoch.season_id,
            player_id=player_id,
            source_time__gte=epoch.starts_at,
            source_time__lt=epoch.ends_at,
            category="CORE_GAMEPLAY",
            source_type__in=CORE_FACT_SOURCES,
        )
        for row in facts.values("source_type").annotate(amount=Sum("amount"), events=Count("pk")):
            sources[row["source_type"]] = {"amount": row["amount"], "events": row["events"]}
        daily = list(
            DailyClaim.objects.filter(
                season_id=epoch.season_id,
                player_id=player_id,
                claimed_at__gte=epoch.starts_at,
                claimed_at__lt=epoch.ends_at,
                reward_ledger__isnull=False,
            ).values_list("claim_period", flat=True)
        )
        contribution = ClanContributionEntry.objects.filter(
            membership__season_id=epoch.season_id,
            membership__player_id=player_id,
            occurred_at__gte=epoch.starts_at,
            occurred_at__lt=epoch.ends_at,
        ).aggregate(contribution=Sum("amount"), events=Count("pk"))
        acquisitions = list(
            PerformanceSourceFact.objects.filter(
                season_id=epoch.season_id,
                player_id=player_id,
                source_type="REFERRAL_ACTIVATION",
                category="VERIFIED_REFERRAL",
                source_time__gte=epoch.starts_at,
                source_time__lt=epoch.ends_at,
            ).values_list("source_id", flat=True)
        )
        inputs = {
            "CLAN_PERFORMANCE": contribution,
            "CORE_GAMEPLAY": {
                "durable_sources": sources,
                "durable_source_amount": sum(v["amount"] for v in sources.values()),
                "durable_source_events": sum(v["events"] for v in sources.values()),
                "daily_periods": daily,
                "player_amount": 0,
                "reward_event_count": 0,
            },
            "CREATOR_COMMUNITY_GROWTH": {},
            "VERIFIED_REFERRAL": {"activated_relations": acquisitions},
            "LOYALTY_CONSISTENCY": {"policy_unconfigured": True},
        }
        for category, raw in inputs.items():
            EpochPerformanceSnapshot.objects.create(
                epoch=epoch,
                category=category,
                player_id=player_id,
                raw_inputs=raw,
                performance_units=0,
                qualification_decision=decision,
                config_version_id=epoch.config_version_id,
            )


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
    from apps.arena.recognition import bound_policy

    selected = bound_policy(row)
    if selected is not None:
        if strategies is not None:
            raise ValidationError("BOUND_POLICY_FORBIDS_CUSTOM_ALLOCATION_STRATEGIES")
        _, economic_policy = selected
        from apps.arena.scoring import emission_limit, proportional_allocation

        def allocate_bound(
            budget: EpochCategoryBudget, snapshots: list[EpochPerformanceSnapshot]
        ) -> dict[int, int]:
            scores = {
                s.player_id: s.performance_units
                for s in snapshots
                if s.performance_units >= economic_policy.number("minimum_category_score")
            }
            h = sum(int(s.raw_inputs.get("h_units", 0)) for s in snapshots if s.player_id in scores)
            limit, _ = emission_limit(
                budget.base_epoch_budget,
                budget.carry_in,
                budget.category,
                scores,
                h,
                economic_policy,
            )
            cap = (
                budget.base_epoch_budget
                * economic_policy.number("player_cap_bps")
                // economic_policy.number("scale")
            )
            return proportional_allocation(scores, limit, cap)

        strategies = {budget.category: allocate_bound for budget in budgets}
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
        reserve = budget.correction_reserve.amount if selected is not None else 0
        budget.carry_out = budget.effective_budget - actual - reserve
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
            == actual.get(budget.category, 0)
            + budget.carry_out
            + budget.residual_amount
            + (budget.correction_reserve.amount if hasattr(budget, "correction_reserve") else 0)
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
    from apps.arena.models import DlxCorrection

    annotations = {
        "held_amount": Sum("adjustments__amount", filter=Q(adjustments__kind="HOLD")),
        "release_amount": Sum("adjustments__amount", filter=Q(adjustments__kind="RELEASE_HOLD")),
        "forfeited_amount": Sum("adjustments__amount", filter=Q(adjustments__kind="FORFEIT")),
        "reversal_amount": Sum("adjustments__amount", filter=Q(adjustments__kind="REVERSAL")),
    }
    fields = ("amount", "held_amount", "release_amount", "forfeited_amount", "reversal_amount")
    normal = (
        DlxAllocation.objects.filter(player=player, season=season)
        .annotate(**annotations)
        .values(*fields)
    )
    corrections = (
        DlxCorrection.objects.filter(
            entitlement__player=player, entitlement__deferral__epoch__season=season
        )
        .annotate(**annotations)
        .values(*fields)
    )
    # UNION ALL is one SQL snapshot and retains equal amounts from distinct credits.
    rows = list(normal.union(corrections, all=True))
    confirmed = sum(row["amount"] for row in rows)
    held = sum((row["held_amount"] or 0) - (row["release_amount"] or 0) for row in rows)
    forfeited = sum((row["forfeited_amount"] or 0) - (row["reversal_amount"] or 0) for row in rows)
    return {
        "confirmed": confirmed,
        "held": held,
        "forfeited": forfeited,
        "net_confirmed": confirmed - forfeited,
        "claimable": 0,
    }


def reconcile_player_balance(player: Player, season: Season, projection: dict[str, int]) -> bool:
    return player_dlx_balance(player, season) == projection
