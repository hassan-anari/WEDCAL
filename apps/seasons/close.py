"""Durable Season-close application workflow.

RabbitMQ is intentionally absent from readiness: durable PostgreSQL source state is the barrier.
Each stage is idempotent and advances only after its own persisted invariants are satisfied.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import structlog
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, models, transaction
from django.db.models import Min, Sum
from django.utils import timezone

from apps.arena.models import (
    AllocationBatch,
    DlxAdjustment,
    DlxAllocation,
    EconomicEpoch,
    EpochAnomaly,
    EpochCategoryBudget,
    RankingProjectionEvent,
    SeasonDlxResidualEvent,
)
from apps.base_tap.models import BaseTapDailyAccumulator
from apps.clans.models import (
    ClanActivity,
    ClanActivityWindow,
    ClanGovernanceCase,
    ClanInvite,
    ClanJoinRequest,
    ClanSeason,
)
from apps.game_config.services import resolve_game_config_version
from apps.missions.models import PlayerMission
from apps.network.models import CreatorWindowSettlement, NetworkActivity
from apps.profile_collection.models import PlayerMedalAward
from apps.referrals.models import ReferralRelation, ReferralShareOutcome
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
from apps.seasons.models import (
    ClanSeasonFinalStanding,
    PlayerSeason,
    PlayerSeasonFinalStanding,
    Season,
    SeasonCloseRun,
)
from apps.telegram.models import TelegramInboundUpdate
from apps.typing_game.models import TypingWallet
from apps.typing_game.services import auto_settle_wallet_for_season

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DrainEvidence:
    provider: str
    pending_count: int
    oldest_source_at: str | None
    oldest_processing_age_seconds: int | None
    blocking: bool
    safe_terminal_state: str
    unresolved_review_count: int
    watermark: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CloseReadiness:
    season_id: int
    season_code: str
    status: str
    cutoff_at: str
    close_window_open: bool
    providers: tuple[DrainEvidence, ...]
    xp_reconciliation: dict[str, int]
    dlx_reconciliation: dict[str, Any]
    blocking_reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.close_window_open and not self.blocking_reasons

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ready": self.ready}


Provider = Callable[[Season, datetime], DrainEvidence]
_PROVIDERS: dict[str, Provider] = {}


def drain_provider(name: str) -> Callable[[Provider], Provider]:
    def register(provider: Provider) -> Provider:
        _PROVIDERS[name] = provider
        return provider

    return register


def _evidence(
    name: str,
    queryset: Any,
    *,
    time_field: str,
    unresolved: int = 0,
    watermark: dict[str, Any] | None = None,
) -> DrainEvidence:
    pending = queryset.count()
    oldest = queryset.aggregate(value=Min(time_field))["value"]
    age = max(int((timezone.now() - oldest).total_seconds()), 0) if oldest else None
    return DrainEvidence(
        name,
        pending,
        oldest.isoformat() if oldest else None,
        age,
        bool(pending or unresolved),
        "EMPTY" if not pending and not unresolved else "PENDING",
        unresolved,
        watermark or {},
    )


@drain_provider("telegram_inbound")
def _telegram(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = TelegramInboundUpdate.objects.filter(received_at__lt=cutoff).exclude(
        state__in=("PROCESSED", "IGNORED")
    )
    return _evidence("telegram_inbound", qs, time_field="received_at")


@drain_provider("network_activity")
def _network(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = NetworkActivity.objects.filter(
        season=season, observed_at__lt=cutoff, window_processed=False
    )
    return _evidence("network_activity", qs, time_field="observed_at")


@drain_provider("creator_windows")
def _creator(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = CreatorWindowSettlement.objects.filter(season=season, window_start__lt=cutoff).filter(
        finalized_at__isnull=True
    )
    return _evidence("creator_windows", qs, time_field="window_start")


@drain_provider("clan_activity")
def _clan_activity(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = ClanActivity.objects.filter(season=season, observed_at__lt=cutoff, window_processed=False)
    return _evidence("clan_activity", qs, time_field="observed_at")


@drain_provider("clan_windows")
def _clan_windows(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = ClanActivityWindow.objects.filter(
        clan_season__season=season, window_end__lte=cutoff
    ).filter(finalized_at__isnull=True)
    return _evidence("clan_windows", qs, time_field="window_start")


@drain_provider("clan_open_work")
def _clan_open_work(season: Season, cutoff: datetime) -> DrainEvidence:
    open_cases = ClanGovernanceCase.objects.filter(
        clan_season__season=season, status=ClanGovernanceCase.Status.OPEN
    ).count()
    unsafe = (
        ClanInvite.objects.filter(
            unknown_credential_risk=True,
        )
        .filter(
            models.Q(join_request__clan_season__season=season) | models.Q(membership__season=season)
        )
        .count()
    )
    return DrainEvidence(
        "clan_open_work",
        0,
        None,
        None,
        bool(open_cases or unsafe),
        "READY_FOR_DISPOSITION" if not open_cases and not unsafe else "MANUAL_REVIEW",
        open_cases + unsafe,
        {
            "pending_join_requests": ClanJoinRequest.objects.filter(
                clan_season__season=season,
                state__in=(ClanJoinRequest.State.PENDING, ClanJoinRequest.State.ACCEPTED),
            ).count(),
            "live_invite_credentials": ClanInvite.objects.filter(
                models.Q(join_request__clan_season__season=season)
                | models.Q(membership__season=season),
                state__in=(ClanInvite.State.ACTIVE, ClanInvite.State.CREATING),
            ).count(),
            "open_governance_cases": open_cases,
            "unsafe_credentials": unsafe,
        },
    )


@drain_provider("reward_outbox")
def _reward_outbox(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = RewardSettlementOutbox.objects.filter(
        reward__season=season, reward__occurred_at__lt=cutoff
    ).exclude(state=RewardSettlementOutbox.State.PROCESSED)
    return _evidence("reward_outbox", qs, time_field="available_at")


@drain_provider("referral_work")
def _referrals(season: Season, cutoff: datetime) -> DrainEvidence:
    reward_ids = RewardLedger.objects.filter(season=season, occurred_at__lt=cutoff).values("pk")
    qs = RewardLedger.objects.filter(pk__in=reward_ids).exclude(
        pk__in=ReferralShareOutcome.objects.values("source_reward_id")
    )
    pending_relations = ReferralRelation.objects.filter(
        status=ReferralRelation.Status.PENDING
    ).count()
    return _evidence("referral_work", qs, time_field="occurred_at", unresolved=pending_relations)


@drain_provider("base_tap")
def _base_tap(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = BaseTapDailyAccumulator.objects.filter(season=season, pending_points__gt=0).exclude(
        settlement_state=BaseTapDailyAccumulator.SettlementState.SETTLED
    )
    return _evidence("base_tap", qs, time_field="first_accepted_at")


@drain_provider("mission_entitlements")
def _missions(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = PlayerMission.objects.filter(season=season, status=PlayerMission.Status.REWARD_READY)
    return _evidence("mission_entitlements", qs, time_field="verified_at")


@drain_provider("typing_wallets")
def _typing(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = TypingWallet.objects.filter(season=season, claimable_balance__gt=0)
    return _evidence("typing_wallets", qs, time_field="updated_at")


@drain_provider("ranking_events")
def _ranking(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = RankingProjectionEvent.objects.filter(season=season, processed_at__isnull=True)
    return _evidence("ranking_events", qs, time_field="created_at")


@drain_provider("economic_epochs")
def _epochs(season: Season, cutoff: datetime) -> DrainEvidence:
    rows = list(EconomicEpoch.objects.filter(season=season).order_by("epoch_number"))
    terminal = len(rows) == 6 and [row.epoch_number for row in rows] == list(range(1, 7))
    pending = sum(row.state != EconomicEpoch.State.SETTLED for row in rows)
    return DrainEvidence(
        "economic_epochs",
        pending,
        None,
        None,
        not terminal or bool(pending),
        "SIX_SETTLED" if terminal and not pending else "INCOMPLETE",
        0,
        {"states": [{"epoch": row.epoch_number, "state": row.state} for row in rows]},
    )


@drain_provider("economic_manual_review")
def _anomalies(season: Season, cutoff: datetime) -> DrainEvidence:
    qs = EpochAnomaly.objects.filter(epoch__season=season)
    open_holds = (
        DlxAdjustment.objects.filter(
            allocation__season=season, kind=DlxAdjustment.Kind.HOLD
        ).count()
        - DlxAdjustment.objects.filter(
            allocation__season=season, kind=DlxAdjustment.Kind.RELEASE_HOLD
        ).count()
    )
    return _evidence(
        "economic_manual_review", qs, time_field="created_at", unresolved=max(open_holds, 0)
    )


def reconcile_season_points(season: Season, *, repair: bool = False) -> dict[str, int]:
    expected_rows = (
        RewardLedger.objects.filter(season=season)
        .values("player_id")
        .annotate(total=Sum("player_amount"))
    )
    expected = {row["player_id"]: int(row["total"] or 0) for row in expected_rows}
    projected = {
        row.player_id: row.season_points for row in PlayerSeason.objects.filter(season=season)
    }
    ids = set(expected) | set(projected)
    drift = sum(1 for player_id in ids if expected.get(player_id, 0) != projected.get(player_id, 0))
    repaired = 0
    if repair:
        with transaction.atomic():
            for player_id in sorted(ids):
                value = expected.get(player_id, 0)
                row, _ = PlayerSeason.objects.select_for_update().get_or_create(
                    season=season, player_id=player_id
                )
                if row.season_points != value:
                    row.season_points = value
                    row.ranking_generation += 1
                    row.save(update_fields=("season_points", "ranking_generation", "updated_at"))
                    repaired += 1
    return {
        "expected_season_points": sum(expected.values()),
        "projected_season_points": sum(projected.values()),
        "drifted_players": drift,
        "repaired_players": repaired,
    }


def reconcile_season_dlx(season: Season, *, record_residuals: bool = False) -> dict[str, Any]:
    config = resolve_game_config_version(season.baseline_config_id)
    policy = config.dlx_distribution
    if policy is None:
        raise ValidationError("Season has no DLX distribution policy")
    epochs = list(EconomicEpoch.objects.filter(season=season).order_by("epoch_number"))
    exact_epochs = len(epochs) == 6 and [row.epoch_number for row in epochs] == list(range(1, 7))
    budgets = EpochCategoryBudget.objects.filter(epoch__season=season)
    authorized_budget = sum(row.base_budget + row.growth_budget for row in epochs)
    allocated_projection = int(budgets.aggregate(value=Sum("allocated_amount"))["value"] or 0)
    allocation_ledger = int(
        DlxAllocation.objects.filter(season=season).aggregate(value=Sum("amount"))["value"] or 0
    )
    residual = int(
        budgets.filter(epoch__epoch_number=6).aggregate(value=Sum("carry_out"))["value"] or 0
    )
    confirmed_batches = AllocationBatch.objects.filter(
        epoch__season=season, confirmed_at__isnull=False
    ).count()
    adjustment_totals = {
        row["kind"]: int(row["value"] or 0)
        for row in DlxAdjustment.objects.filter(allocation__season=season)
        .values("kind")
        .annotate(value=Sum("amount"))
    }
    if record_residuals:
        for row in budgets.filter(epoch__epoch_number=6).select_related("config_version"):
            amount = row.carry_out
            SeasonDlxResidualEvent.objects.get_or_create(
                season=season,
                category=row.category,
                defaults={
                    "amount": amount,
                    "provenance": {"epoch_category_budget_id": row.pk},
                    "config_version": row.config_version,
                    "disposition": "UNALLOCATED_SEASON_RESIDUAL",
                    "reason": "Final same-category carry has no seventh economic epoch",
                    "idempotency_key": f"season-close-residual:{season.pk}:{row.category}",
                },
            )
    return {
        "exactly_six_epochs": exact_epochs,
        "epoch_states": [row.state for row in epochs],
        "base_pool": policy.base_pool_dlx_amount,
        "growth_unlocks": sum(row.growth_budget for row in epochs),
        "authorized_budget": authorized_budget,
        "projected_allocations": allocated_projection,
        "confirmed_allocations": allocation_ledger,
        "final_residual": residual,
        "confirmed_batches": confirmed_batches,
        "adjustments": adjustment_totals,
        "xp_events_with_dlx_source": RewardLedger.objects.filter(
            season=season, source_type__icontains="DLX"
        ).count(),
        "reconciled": exact_epochs
        and confirmed_batches == 6
        and allocated_projection == allocation_ledger
        and allocation_ledger + residual == authorized_budget
        and not RewardLedger.objects.filter(season=season, source_type__icontains="DLX").exists(),
    }


def preview_close(season: Season, *, at: datetime | None = None) -> CloseReadiness:
    now = at or timezone.now()
    from apps.seasons.services import economic_window_end

    cutoff = economic_window_end(season)
    providers = tuple(provider(season, cutoff) for provider in _PROVIDERS.values())
    xp = reconcile_season_points(season)
    dlx = reconcile_season_dlx(season)
    blockers = [item.provider for item in providers if item.blocking]
    if xp["drifted_players"]:
        blockers.append("xp_projection_drift")
    if not dlx["reconciled"]:
        blockers.append("dlx_reconciliation")
    if season.status != Season.Status.ACTIVE:
        blockers.append("season_not_active")
    return CloseReadiness(
        season.pk,
        season.code,
        season.status,
        cutoff.isoformat(),
        now >= cutoff,
        providers,
        xp,
        dlx,
        tuple(blockers),
    )


@transaction.atomic
def start_or_resume_close(
    season: Season,
    *,
    actor: User | None,
    service_actor: str = "",
    reason: str,
    ticket_reference: str = "",
    correlation_id: uuid.UUID | None = None,
) -> SeasonCloseRun:
    if actor is not None and not actor.has_perm("seasons.close_season"):
        raise PermissionDenied("Season-close permission required")
    if not reason.strip() or (actor is None and not service_actor.strip()):
        raise ValidationError("A close actor and reason are required")
    season = Season.objects.select_for_update().get(pk=season.pk)
    if season.status != Season.Status.ACTIVE:
        raise ValidationError("Only an ACTIVE Season can start closing")
    from apps.seasons.services import economic_window_end

    cutoff = economic_window_end(season)
    if timezone.now() < cutoff:
        raise ValidationError("The economic cutoff has not been reached")
    run, created = SeasonCloseRun.objects.get_or_create(
        season=season,
        defaults={
            "idempotency_key": f"season-close:{season.pk}:{cutoff.isoformat()}",
            "cutoff_at": cutoff,
            "actor": actor,
            "service_actor": service_actor if actor is None else "",
            "reason": reason,
            "ticket_reference": ticket_reference,
            "correlation_id": correlation_id or uuid.uuid4(),
        },
    )
    if not created and run.stage == SeasonCloseRun.Stage.FAILED:
        run.retry_count += 1
        run.stage = run.failure_stage or SeasonCloseRun.Stage.PREPARING
        run.failed_at = None
        run.failure_category = ""
        run.save()
    transaction.on_commit(lambda: _enqueue(run.pk))
    return run


def _enqueue(run_id: int) -> None:
    from apps.seasons.tasks import advance_season_close

    advance_season_close.delay(run_id)


def _settle_ready_missions(season: Season, limit: int) -> int:
    rows = list(
        PlayerMission.objects.filter(season=season, status=PlayerMission.Status.REWARD_READY)
        .select_related("player", "definition", "config_version")
        .order_by("pk")[:limit]
    )
    from apps.game_config.services import resolve_effective_game_config
    from apps.rewards.services import settle_player_reward

    for progress in rows:
        assert progress.verified_at and progress.config_version_id
        resolved = resolve_effective_game_config(progress.verified_at)
        with transaction.atomic():
            locked = PlayerMission.objects.select_for_update().get(pk=progress.pk)
            if locked.status != PlayerMission.Status.REWARD_READY:
                continue
            if progress.reward_amount is None:
                raise ValidationError("Ready mission has no durable reward entitlement")
            ledger, _ = settle_player_reward(
                player=progress.player,
                source_type="MISSION",
                source_id=f"{progress.definition.key}:v{progress.definition.version}:{progress.eligibility_period}",
                context="MISSION",
                amount=int(progress.reward_amount),
                idempotency_key=f"mission:{progress.player_id}:{progress.definition_id}:{progress.eligibility_period}",
                occurred_at=progress.verified_at,
                resolved_config=resolved,
                source_season=season,
                preserve_config_version=True,
                source_eligibility_verified=True,
            )
            locked.status = PlayerMission.Status.CLAIMED
            locked.claimed_at = timezone.now()
            locked.reward_ledger = ledger
            locked.save(update_fields=("status", "claimed_at", "reward_ledger", "updated_at"))
    return len(rows)


def freeze_standings(season: Season, *, batch_size: int = 1000) -> dict[str, int]:
    if RankingProjectionEvent.objects.filter(season=season, processed_at__isnull=True).exists():
        raise ValidationError("Ranking projection events remain pending")
    now = timezone.now()
    player_offset = PlayerSeasonFinalStanding.objects.filter(season=season).count()
    clan_offset = ClanSeasonFinalStanding.objects.filter(season=season).count()
    players = list(
        PlayerSeason.objects.filter(season=season, season_points__gt=0).order_by(
            "-season_points", "score_reached_at", "player_id"
        )[player_offset : player_offset + batch_size]
    )
    clans = list(
        ClanSeason.objects.filter(season=season, competitive_score__gt=0).order_by(
            "-competitive_score", "score_reached_at", "pk"
        )[clan_offset : clan_offset + batch_size]
    )
    player_population = PlayerSeason.objects.filter(season=season, season_points__gt=0).count()
    clan_population = ClanSeason.objects.filter(season=season, competitive_score__gt=0).count()
    PlayerSeasonFinalStanding.objects.bulk_create(
        [
            PlayerSeasonFinalStanding(
                season=season,
                player_id=row.player_id,
                final_rank=rank,
                final_season_points=row.season_points,
                score_reached_at=row.score_reached_at or season.starts_at,
                ranking_generation=row.ranking_generation,
                ranked_population=player_population,
                provenance={"projection": "PlayerSeason", "projection_id": row.pk},
                finalized_at=now,
            )
            for rank, row in enumerate(players, player_offset + 1)
        ],
        ignore_conflicts=True,
    )
    ClanSeasonFinalStanding.objects.bulk_create(
        [
            ClanSeasonFinalStanding(
                season=season,
                clan_season=row,
                clan_id=row.clan_id,
                final_rank=rank,
                final_competitive_score=row.competitive_score,
                score_reached_at=row.score_reached_at or season.starts_at,
                ranking_generation=row.ranking_generation,
                ranked_population=clan_population,
                provenance={"projection": "ClanSeason", "projection_id": row.pk},
                finalized_at=now,
            )
            for rank, row in enumerate(clans, clan_offset + 1)
        ],
        ignore_conflicts=True,
    )
    frozen_players = PlayerSeasonFinalStanding.objects.filter(season=season).count()
    frozen_clans = ClanSeasonFinalStanding.objects.filter(season=season).count()
    return {
        "players": player_population,
        "clans": clan_population,
        "frozen_players": frozen_players,
        "frozen_clans": frozen_clans,
        "complete": int(frozen_players == player_population and frozen_clans == clan_population),
    }


def award_season_honors(season: Season, *, limit: int = 1000) -> dict[str, int]:
    config = resolve_game_config_version(season.baseline_config_id)
    if config.season_honors is None:
        raise ValidationError("Season honors configuration is unavailable")
    thresholds = {rule.key: rule.threshold for rule in config.season_honors.medals}
    rows = (
        PlayerSeasonFinalStanding.objects.filter(season=season)
        .exclude(
            player__medal_awards__source_season=season,
            player__medal_awards__medal_key="season-founding",
        )
        .order_by("final_rank")[:limit]
    )
    created = 0
    for row in rows:
        keys = ["season-founding"]
        if row.final_season_points >= int(thresholds["season-participant"] or 0):
            keys.append("season-participant")
        if row.final_season_points >= int(thresholds["season-veteran"] or 0):
            keys.append("season-veteran")
        # Integer inclusion: rank * 10_000 <= population * configured percentile bps.
        if row.final_rank * 10_000 <= row.ranked_population * int(thresholds["season-top-10"] or 0):
            keys.append("season-top-10")
        if row.final_rank * 10_000 <= row.ranked_population * int(thresholds["season-top-1"] or 0):
            keys.append("season-top-1")
        if row.final_rank <= int(thresholds["season-champion"] or 0):
            keys.append("season-champion")
        for key in keys:
            _, was_created = PlayerMedalAward.objects.get_or_create(
                player_id=row.player_id,
                medal_key=key,
                source_season=season,
                defaults={
                    "awarded_at": timezone.now(),
                    "config_version_id": config.id,
                    "reason": f"SEASON_CLOSE:{row.final_rank}:{row.ranked_population}",
                },
            )
            created += int(was_created)
    remaining = (
        PlayerSeasonFinalStanding.objects.filter(season=season)
        .exclude(
            player__medal_awards__source_season=season,
            player__medal_awards__medal_key="season-founding",
        )
        .count()
    )
    return {"created": created, "remaining": remaining}


def finalize_clans(season: Season) -> int:
    # Historical rows are retained. Credentials become non-authorizing because all join paths
    # already require an ACTIVE Season/ClanSeason; states remain available for audit/revocation.
    ClanJoinRequest.objects.filter(
        clan_season__season=season,
        state__in=(ClanJoinRequest.State.PENDING, ClanJoinRequest.State.ACCEPTED),
    ).update(state=ClanJoinRequest.State.CANCELLED, decided_at=timezone.now())
    ClanInvite.objects.filter(
        models.Q(join_request__clan_season__season=season) | models.Q(membership__season=season),
        state__in=(ClanInvite.State.ACTIVE, ClanInvite.State.CREATING, ClanInvite.State.PENDING),
        unknown_credential_risk=False,
    ).update(state=ClanInvite.State.REVOKE_PENDING, desired_terminal_state=ClanInvite.State.REVOKED)
    return (
        ClanSeason.objects.filter(season=season)
        .exclude(state=ClanSeason.State.ENDED)
        .update(state=ClanSeason.State.ENDED, accepting_members=False)
    )


def advance_close(run_id: int, *, batch_size: int = 500) -> str:
    with transaction.atomic():
        run = SeasonCloseRun.objects.select_for_update().select_related("season").get(pk=run_id)
        if run.stage == SeasonCloseRun.Stage.COMPLETED:
            return run.stage
        season = run.season
        try:
            if run.stage == SeasonCloseRun.Stage.PREPARING:
                report = preview_close(season)
                run.readiness_evidence = report.as_dict()
                run.stage = SeasonCloseRun.Stage.DRAINING
            elif run.stage == SeasonCloseRun.Stage.DRAINING:
                wallet_ids = list(
                    TypingWallet.objects.filter(season=season, claimable_balance__gt=0)
                    .order_by("pk")
                    .values_list("pk", flat=True)[:batch_size]
                )
                for wallet_id in wallet_ids:
                    auto_settle_wallet_for_season(wallet_id, close_run_id=run.pk, at=timezone.now())
                _settle_ready_missions(season, batch_size)
                report = preview_close(season)
                run.drain_evidence = {item.provider: asdict(item) for item in report.providers}
                if report.blocking_reasons:
                    run.save()
                    return run.stage
                run.stage = SeasonCloseRun.Stage.RECONCILING
            elif run.stage == SeasonCloseRun.Stage.RECONCILING:
                xp = reconcile_season_points(season, repair=True)
                dlx = reconcile_season_dlx(season, record_residuals=True)
                run.reconciliation_evidence = {"xp": xp, "dlx": dlx}
                if xp["drifted_players"] and xp["repaired_players"] != xp["drifted_players"]:
                    raise ValidationError("XP reconciliation did not converge")
                if not dlx["reconciled"]:
                    raise ValidationError("DLX reconciliation did not converge")
                run.stage = SeasonCloseRun.Stage.FREEZING_RANKINGS
            elif run.stage == SeasonCloseRun.Stage.FREEZING_RANKINGS:
                standings = freeze_standings(season, batch_size=batch_size)
                run.progress = {**run.progress, "standings": standings}
                if standings["complete"]:
                    run.stage = SeasonCloseRun.Stage.AWARDING_HONORS
            elif run.stage == SeasonCloseRun.Stage.AWARDING_HONORS:
                honors = award_season_honors(season, limit=batch_size)
                run.progress = {**run.progress, "honors": honors}
                if not honors["remaining"]:
                    run.stage = SeasonCloseRun.Stage.FINALIZING
            elif run.stage == SeasonCloseRun.Stage.FINALIZING:
                run.progress = {**run.progress, "clans_ended": finalize_clans(season)}
                report = preview_close(season)
                remaining = [x for x in report.blocking_reasons if x != "season_not_active"]
                if remaining:
                    raise ValidationError(f"Final close blockers remain: {remaining}")
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('dexo.season_transition', 'on', true)")
                season.status = Season.Status.CLOSED
                season.save(update_fields=("status", "updated_at"))
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('dexo.season_transition', 'off', true)")
                run.stage = SeasonCloseRun.Stage.COMPLETED
                run.completed_at = timezone.now()
            run.save()
        except Exception as exc:
            run.failure_stage = run.stage
            run.failure_category = type(exc).__name__
            run.failed_at = timezone.now()
            run.stage = SeasonCloseRun.Stage.FAILED
            run.save()
            logger.exception(
                "season_close_stage_failed",
                season_id=season.pk,
                close_run_id=run.pk,
                failure_category=type(exc).__name__,
            )
            raise
    if run.stage != SeasonCloseRun.Stage.COMPLETED:
        _enqueue(run.pk)
    logger.info("season_close_advanced", season_id=season.pk, close_run_id=run.pk, stage=run.stage)
    return run.stage
