"""Durable Season-close application workflow.

RabbitMQ is intentionally absent from readiness: durable PostgreSQL source state is the barrier.
Each stage is idempotent and advances only after its own persisted invariants are satisfied.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import structlog
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, models, transaction
from django.db.models import Max, Min, Sum
from django.utils import timezone

from apps.arena.models import (
    AllocationBatch,
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
from apps.referrals.models import ReferralShareOutcome
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
    relevant = TelegramInboundUpdate.objects.filter(received_at__lt=cutoff)
    transient = relevant.filter(state__in=("PENDING", "PROCESSING", "FAILED"))
    dead = relevant.filter(state=TelegramInboundUpdate.State.DEAD_LETTER)
    oldest = relevant.filter(
        models.Q(state__in=("PENDING", "PROCESSING", "FAILED"))
        | models.Q(state=TelegramInboundUpdate.State.DEAD_LETTER)
    ).aggregate(value=Min("received_at"))["value"]
    transient_count = transient.count()
    dead_count = dead.count()
    return DrainEvidence(
        "telegram_inbound",
        transient_count + dead_count,
        oldest.isoformat() if oldest else None,
        max(int((timezone.now() - oldest).total_seconds()), 0) if oldest else None,
        bool(transient_count or dead_count),
        "MANUAL_REVIEW" if dead_count else ("PENDING" if transient_count else "EMPTY"),
        dead_count,
        {"transient_count": transient_count, "dead_letter_count": dead_count},
    )


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


@drain_provider("clan_mission_signals")
def _clan_mission_signals(season: Season, cutoff: datetime) -> DrainEvidence:
    from apps.clans.models import ClanMissionSignal

    qs = ClanMissionSignal.objects.filter(
        membership__season=season, occurred_at__lt=cutoff, processed_at__isnull=True
    )
    return _evidence("clan_mission_signals", qs, time_field="occurred_at")


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
    eligible_sources = resolve_game_config_version(
        season.baseline_config_id
    ).referral.eligible_reward_sources
    qs = RewardLedger.objects.filter(
        season=season, occurred_at__lt=cutoff, source_type__in=eligible_sources
    ).exclude(pk__in=ReferralShareOutcome.objects.values("source_reward_id"))
    return _evidence("referral_work", qs, time_field="occurred_at")


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
    from apps.arena.adjustments import season_adjustment_totals

    qs = EpochAnomaly.objects.filter(epoch__season=season, resolution__isnull=True)
    return _evidence(
        "economic_manual_review",
        qs,
        time_field="created_at",
        unresolved=qs.count(),
        watermark={"dlx_adjustments": season_adjustment_totals(season)},
    )


def reconcile_season_points(
    season: Season, *, repair: bool = False, after_player_id: int = 0, limit: int = 500
) -> dict[str, int]:
    """Bounded adapter over the canonical all-Season RewardLedger projection rebuild."""
    from apps.players.models import Player
    from apps.progression.models import PlayerProgression
    from apps.rewards.services import rebuild_player_reward_projections

    player_ids = list(
        Player.objects.filter(
            models.Q(reward_ledger__season=season) | models.Q(seasons__season=season),
            pk__gt=after_player_id,
        )
        .order_by("pk")
        .values_list("pk", flat=True)
        .distinct()[:limit]
    )
    numeric_drifted = tie_drifted = drifted = repaired = 0
    from apps.game_config.services import resolve_effective_game_config

    config = resolve_effective_game_config(timezone.now())
    for player_id in player_ids:
        season_source = RewardLedger.objects.filter(season=season, player_id=player_id).aggregate(
            value=Sum("player_amount"), reached_at=Max("occurred_at")
        )
        expected_season = int(season_source["value"] or 0)
        expected_reached_at = season_source["reached_at"] if expected_season else None
        expected_lifetime = int(
            RewardLedger.objects.filter(player_id=player_id).aggregate(value=Sum("player_amount"))[
                "value"
            ]
            or 0
        )
        projection = (
            PlayerSeason.objects.filter(season=season, player_id=player_id)
            .values("season_points", "score_reached_at")
            .first()
        )
        projected_season = int(projection["season_points"]) if projection else 0
        projected_reached_at = projection["score_reached_at"] if projection else None
        projected_lifetime = (
            PlayerProgression.objects.filter(player_id=player_id)
            .values_list("lifetime_points", flat=True)
            .first()
            or 0
        )
        numeric_drift = (
            expected_season != projected_season or expected_lifetime != projected_lifetime
        )
        tie_drift = projected_reached_at != expected_reached_at
        numeric_drifted += int(numeric_drift)
        tie_drifted += int(tie_drift)
        drift = numeric_drift or tie_drift
        drifted += int(drift)
        if repair and drift:
            rebuild_player_reward_projections(Player.objects.get(pk=player_id), config)
            repaired_projection = (
                PlayerSeason.objects.filter(season=season, player_id=player_id)
                .values("season_points", "score_reached_at")
                .first()
            )
            repaired_lifetime = PlayerProgression.objects.get(player_id=player_id).lifetime_points
            if (
                repaired_projection is None
                or repaired_projection["season_points"] != expected_season
                or repaired_projection["score_reached_at"] != expected_reached_at
                or repaired_lifetime != expected_lifetime
            ):
                raise ValidationError("Canonical Player reward projection rebuild did not converge")
            repaired += 1
    return {
        "expected_season_points": int(
            RewardLedger.objects.filter(season=season).aggregate(value=Sum("player_amount"))[
                "value"
            ]
            or 0
        ),
        "projected_season_points": int(
            PlayerSeason.objects.filter(season=season).aggregate(value=Sum("season_points"))[
                "value"
            ]
            or 0
        ),
        "expected_lifetime_points": int(
            RewardLedger.objects.aggregate(value=Sum("player_amount"))["value"] or 0
        ),
        "projected_lifetime_points": int(
            PlayerProgression.objects.aggregate(value=Sum("lifetime_points"))["value"] or 0
        ),
        "numeric_drifted_players": numeric_drifted,
        "tie_provenance_drifted_players": tie_drifted,
        "drifted_players": drifted,
        "repaired_players": repaired,
        "processed_players": len(player_ids),
        "next_player_id": player_ids[-1] if player_ids else after_player_id,
        "complete": int(len(player_ids) < limit),
    }


def reconcile_clan_projections(
    season: Season, *, repair: bool = False, after_clan_season_id: int = 0, limit: int = 500
) -> dict[str, int]:
    """Bounded verification through the canonical Clan projection calculator/rebuilder."""
    from apps.clans.economy import canonical_clan_projection, rebuild_clan_projections

    rows = list(
        ClanSeason.objects.filter(season=season, pk__gt=after_clan_season_id).order_by("pk")[:limit]
    )
    drifted = tie_drifted = repaired = 0
    for clan_season in rows:
        expected = canonical_clan_projection(clan_season)
        fields = tuple(expected)
        drift = any(getattr(clan_season, field) != expected[field] for field in fields)
        tie_drift = clan_season.score_reached_at != expected["score_reached_at"]
        drifted += int(drift)
        tie_drifted += int(tie_drift)
        if repair and drift:
            rebuilt = rebuild_clan_projections(clan_season)
            if any(getattr(rebuilt, field) != expected[field] for field in fields):
                raise ValidationError("Canonical Clan projection rebuild did not converge")
            repaired += 1
    return {
        "drifted_clans": drifted,
        "tie_provenance_drifted_clans": tie_drifted,
        "repaired_clans": repaired,
        "processed_clans": len(rows),
        "next_clan_season_id": rows[-1].pk if rows else after_clan_season_id,
        "complete": int(len(rows) < limit),
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
    from apps.arena.adjustments import season_adjustment_totals

    adjustment_totals = season_adjustment_totals(season)
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


def _enqueue(run_id: int, *, countdown: int | None = None) -> None:
    from apps.seasons.tasks import advance_season_close

    advance_season_close.apply_async(args=(run_id,), countdown=countdown)


def _settle_ready_missions(season: Season, limit: int) -> int:
    rows = list(
        PlayerMission.objects.filter(season=season, status=PlayerMission.Status.REWARD_READY)
        .select_related("player", "definition", "config_version")
        .order_by("pk")[:limit]
    )
    from apps.rewards.services import settle_player_reward

    for progress in rows:
        assert progress.verified_at and progress.config_version_id
        resolved = resolve_game_config_version(progress.config_version_id)
        rule = resolved.missions.definition(progress.definition.key)
        if rule.reward_points != progress.reward_amount:
            raise ValidationError("Mission entitlement does not match its stored configuration")
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
    if PlayerSeason.objects.filter(
        season=season, season_points__gt=0, score_reached_at__isnull=True
    ).exists():
        raise ValidationError("Positive Player score lacks canonical tie provenance")
    if ClanSeason.objects.filter(
        season=season, competitive_score__gt=0, score_reached_at__isnull=True
    ).exists():
        raise ValidationError("Positive Clan score lacks canonical tie provenance")
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
                score_reached_at=cast(datetime, row.score_reached_at),
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
                score_reached_at=cast(datetime, row.score_reached_at),
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


def finalize_clans(season: Season, *, limit: int = 200) -> dict[str, int]:
    from apps.clans.services import close_season_clan_work

    return close_season_clan_work(season.pk, limit=limit)


def _advance_close_stage(run_id: int, *, batch_size: int) -> tuple[str, str]:
    with transaction.atomic():
        run = SeasonCloseRun.objects.select_for_update().select_related("season").get(pk=run_id)
        if run.stage == SeasonCloseRun.Stage.COMPLETED:
            return run.stage, "STOP"
        season = run.season
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
            missions_settled = _settle_ready_missions(season, batch_size)
            report = preview_close(season)
            run.drain_evidence = {item.provider: asdict(item) for item in report.providers}
            if wallet_ids or missions_settled:
                run.blocker_kind = "TRANSIENT"
                run.next_recheck_at = timezone.now()
                run.save()
                return run.stage, "IMMEDIATE"
            manual = any(
                item.blocking and item.unresolved_review_count for item in report.providers
            )
            managed = {"xp_projection_drift"}
            blockers = [reason for reason in report.blocking_reasons if reason not in managed]
            if blockers:
                run.blocker_kind = "MANUAL" if manual else "TRANSIENT"
                run.next_recheck_at = (
                    None
                    if manual
                    else timezone.now() + timedelta(seconds=settings.SEASON_CLOSE_RECHECK_SECONDS)
                )
                run.save()
                return run.stage, "STOP" if manual else "DELAY"
            from apps.seasons.barrier import acquire_close_barrier

            acquire_close_barrier(season.pk)
            # Shared writers that began before us have committed before this exclusive lock returns.
            # Recheck every durable provider while still holding the exclusive transaction lock.
            report = preview_close(season)
            run.drain_evidence = {item.provider: asdict(item) for item in report.providers}
            blockers = [reason for reason in report.blocking_reasons if reason not in managed]
            if blockers:
                run.blocker_kind = (
                    "MANUAL"
                    if any(
                        item.blocking and item.unresolved_review_count for item in report.providers
                    )
                    else "TRANSIENT"
                )
                run.next_recheck_at = (
                    None
                    if run.blocker_kind == "MANUAL"
                    else (timezone.now() + timedelta(seconds=settings.SEASON_CLOSE_RECHECK_SECONDS))
                )
                run.save()
                return run.stage, "STOP" if run.blocker_kind == "MANUAL" else "DELAY"
            with connection.cursor() as barrier_cursor:
                barrier_cursor.execute("SELECT set_config('dexo.season_close_barrier', 'on', true)")
            run.barrier_established_at = timezone.now()
            run.blocker_kind = ""
            run.next_recheck_at = None
            run.stage = SeasonCloseRun.Stage.RECONCILING
        elif run.stage == SeasonCloseRun.Stage.RECONCILING:
            projection_cursor = int(run.progress.get("xp_player_cursor", 0))
            xp = reconcile_season_points(
                season, repair=True, after_player_id=projection_cursor, limit=batch_size
            )
            clan_cursor = int(run.progress.get("clan_season_cursor", 0))
            clan = reconcile_clan_projections(
                season, repair=True, after_clan_season_id=clan_cursor, limit=batch_size
            )
            run.progress = {
                **run.progress,
                "xp_player_cursor": xp["next_player_id"],
                "xp_drifted_players": int(run.progress.get("xp_drifted_players", 0))
                + xp["drifted_players"],
                "xp_repaired_players": int(run.progress.get("xp_repaired_players", 0))
                + xp["repaired_players"],
                "clan_season_cursor": clan["next_clan_season_id"],
                "clan_drifted": int(run.progress.get("clan_drifted", 0)) + clan["drifted_clans"],
                "clan_repaired": int(run.progress.get("clan_repaired", 0)) + clan["repaired_clans"],
            }
            dlx = reconcile_season_dlx(season, record_residuals=True)
            run.reconciliation_evidence = {
                "xp_batch": xp,
                "clan_batch": clan,
                "cumulative": {
                    "xp_numeric_drifted": int(run.progress.get("xp_numeric_drifted", 0))
                    + xp["numeric_drifted_players"],
                    "xp_tie_drifted": int(run.progress.get("xp_tie_drifted", 0))
                    + xp["tie_provenance_drifted_players"],
                    "xp_repaired": run.progress["xp_repaired_players"],
                    "clan_drifted": run.progress["clan_drifted"],
                    "clan_tie_drifted": int(run.progress.get("clan_tie_drifted", 0))
                    + clan["tie_provenance_drifted_clans"],
                    "clan_repaired": run.progress["clan_repaired"],
                },
                "dlx": dlx,
            }
            run.progress = {
                **run.progress,
                "xp_numeric_drifted": run.reconciliation_evidence["cumulative"][
                    "xp_numeric_drifted"
                ],
                "xp_tie_drifted": run.reconciliation_evidence["cumulative"]["xp_tie_drifted"],
                "clan_tie_drifted": run.reconciliation_evidence["cumulative"]["clan_tie_drifted"],
            }
            if xp["drifted_players"] and xp["repaired_players"] != xp["drifted_players"]:
                raise ValidationError("XP reconciliation did not converge")
            if not dlx["reconciled"]:
                raise ValidationError("DLX reconciliation did not converge")
            if clan["drifted_clans"] and clan["repaired_clans"] != clan["drifted_clans"]:
                raise ValidationError("Clan reconciliation did not converge")
            if xp["complete"] and clan["complete"]:
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
            clan_result = finalize_clans(season, limit=batch_size)
            run.progress = {**run.progress, "clans": clan_result}
            if clan_result["unresolved"]:
                run.blocker_kind = (
                    "MANUAL"
                    if _clan_open_work(season, run.cutoff_at).unresolved_review_count
                    else "TRANSIENT"
                )
                run.next_recheck_at = (
                    None
                    if run.blocker_kind == "MANUAL"
                    else (timezone.now() + timedelta(seconds=settings.SEASON_CLOSE_RECHECK_SECONDS))
                )
                run.save()
                return run.stage, "STOP" if run.blocker_kind == "MANUAL" else "DELAY"
            report = preview_close(season)
            remaining = [
                item
                for item in report.blocking_reasons
                if item not in {"season_not_active", "xp_projection_drift"}
            ]
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
        if run.barrier_established_at is not None:
            with connection.cursor() as barrier_cursor:
                barrier_cursor.execute(
                    "SELECT set_config('dexo.season_close_barrier', 'off', true)"
                )
        return run.stage, "STOP" if run.stage == SeasonCloseRun.Stage.COMPLETED else "IMMEDIATE"


def _persist_close_failure(run_id: int, failed_stage: str, exc: Exception) -> None:
    with transaction.atomic():
        run = SeasonCloseRun.objects.select_for_update().get(pk=run_id)
        run.failure_stage = failed_stage
        run.failure_category = type(exc).__name__
        run.failed_at = timezone.now()
        run.stage = SeasonCloseRun.Stage.FAILED
        run.next_recheck_at = None
        run.blocker_kind = ""
        run.save()


def advance_close(run_id: int, *, batch_size: int = 500) -> str:
    failed_stage = SeasonCloseRun.objects.values_list("stage", flat=True).get(pk=run_id)
    try:
        stage, scheduling = _advance_close_stage(run_id, batch_size=batch_size)
    except Exception as exc:
        _persist_close_failure(run_id, failed_stage, exc)
        logger.exception(
            "season_close_stage_failed",
            close_run_id=run_id,
            failure_stage=failed_stage,
            failure_category=type(exc).__name__,
        )
        raise
    if scheduling == "IMMEDIATE":
        _enqueue(run_id)
    elif scheduling == "DELAY":
        _enqueue(run_id, countdown=settings.SEASON_CLOSE_RECHECK_SECONDS)
    logger.info("season_close_advanced", close_run_id=run_id, stage=stage)
    return stage
