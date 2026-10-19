from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.base_tap.models import BaseTapDailyAccumulator, BaseTapReceipt
from apps.game_config.services import resolve_effective_game_config, resolve_game_config_version
from apps.players.models import Player
from apps.rewards.services import settle_player_reward
from apps.seasons.models import Season
from apps.seasons.services import require_reward_earning_time, resolve_current_season

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BaseTapResult:
    created: bool
    accepted_taps: int
    rejected_taps: int
    earned_points: int
    daily_accepted_taps: int
    daily_remaining_taps: int
    pending_points: int
    settlement_status: str


def submit_tap_batch(
    player: Player, *, tap_count: int, idempotency_key: str, at: datetime
) -> BaseTapResult:
    if not idempotency_key or len(idempotency_key) > 100:
        raise ValueError("A bounded Idempotency-Key is required")
    if type(tap_count) is not int or tap_count <= 0:
        raise ValueError("tap_count must be a positive integer")
    if not settings.BASE_TAP_LEGACY_INGRESS_ENABLED:
        # Cutover pause rejects new intents but allows lost-response retrieval of durable receipts.
        with transaction.atomic():
            locked = Player.objects.select_for_update().get(pk=player.pk)
            prior = (
                BaseTapReceipt.objects.select_related("accumulator")
                .filter(player=locked, idempotency_key=idempotency_key)
                .first()
            )
            if prior:
                if prior.requested_taps != tap_count:
                    raise ValueError("Idempotency-Key belongs to another legacy request")
                bound = resolve_game_config_version(prior.config_version_id)
                return _result(prior.accumulator, bound.base_tap.rewarded_taps_per_day, prior)
    config = resolve_effective_game_config(at)
    if not settings.BASE_TAP_LEGACY_INGRESS_ENABLED or config.base_tap.protocol_version != 1:
        raise LookupError("Legacy Base Tap ingress is paused")
    if not config.base_tap.enabled:
        raise LookupError("Base Tap is unavailable")
    if tap_count > config.base_tap.max_taps_per_batch:
        raise ValueError("tap_count exceeds the configured batch limit")
    season = resolve_current_season(at=at)
    assert season is not None
    require_reward_earning_time(season, at)
    utc_day = at.astimezone(UTC).date()
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier

        acquire_economic_write_barrier(season.pk)
        locked_player = Player.objects.select_for_update().get(pk=player.pk)
        from apps.players.services import reward_gameplay_allowed

        if not reward_gameplay_allowed(locked_player):
            raise ValueError("Player moderation state blocks reward generation")
        existing = (
            BaseTapReceipt.objects.select_related("accumulator")
            .filter(player=locked_player, idempotency_key=idempotency_key)
            .first()
        )
        if existing:
            if (
                existing.requested_taps != tap_count
                or existing.accumulator.season_id != season.pk
                or existing.accumulator.utc_day != utc_day
                or existing.config_version_id != config.id
            ):
                raise ValueError("Idempotency-Key cannot cross a Season, day, or config boundary")
            return _result(existing.accumulator, config.base_tap.rewarded_taps_per_day, existing)
        accumulator, created = BaseTapDailyAccumulator.objects.select_for_update().get_or_create(
            player=locked_player,
            season=season,
            utc_day=utc_day,
            defaults={"config_version_id": config.id, "next_attempt_at": at},
        )
        if not created and accumulator.config_version_id != config.id:
            return _zero_result(accumulator, tap_count, config.base_tap.rewarded_taps_per_day)
        rate_limited = bool(
            accumulator.last_batch_at
            and (at - accumulator.last_batch_at).total_seconds() * 1000
            < config.base_tap.min_batch_interval_ms
        )
        available = max(config.base_tap.rewarded_taps_per_day - accumulator.accepted_taps, 0)
        accepted = 0 if rate_limited else min(tap_count, available)
        if accepted == 0:
            return _zero_result(accumulator, tap_count, config.base_tap.rewarded_taps_per_day)
        earned = accepted * config.base_tap.points_per_rewarded_tap
        receipt = BaseTapReceipt.objects.create(
            player=locked_player,
            accumulator=accumulator,
            config_version_id=config.id,
            idempotency_key=idempotency_key,
            requested_taps=tap_count,
            accepted_taps=accepted,
            rejected_taps=tap_count - accepted,
            earned_points=earned,
            accepted_at=at,
            expires_at=at + timedelta(days=config.base_tap.receipt_retention_days),
        )
        from apps.arena.models import PerformanceSourceFact

        performance_fact = PerformanceSourceFact.objects.create(
            season=season,
            player=locked_player,
            category="CORE_GAMEPLAY",
            source_type="BASE_TAP_BATCH",
            source_id=str(receipt.pk),
            source_time=at,
            amount=earned,
            config_version_id=config.id,
            metadata={"accepted_taps": accepted, "utc_day": str(utc_day)},
        )
        from apps.arena.recognition import record_core_fact

        record_core_fact(performance_fact)
        accumulator.accepted_taps += accepted
        accumulator.pending_points += earned
        if accumulator.first_accepted_at is None:
            accumulator.first_accepted_at = at
        accumulator.last_batch_at = at
        accumulator.save(
            update_fields=(
                "accepted_taps",
                "pending_points",
                "first_accepted_at",
                "last_batch_at",
                "updated_at",
            )
        )
    return _result(accumulator, config.base_tap.rewarded_taps_per_day, receipt, created=True)


def _zero_result(
    accumulator: BaseTapDailyAccumulator, requested: int, daily_cap: int
) -> BaseTapResult:
    return BaseTapResult(
        False,
        0,
        requested,
        0,
        accumulator.accepted_taps,
        max(daily_cap - accumulator.accepted_taps, 0),
        accumulator.pending_points,
        accumulator.settlement_state,
    )


def _result(
    accumulator: BaseTapDailyAccumulator,
    daily_cap: int,
    receipt: BaseTapReceipt,
    *,
    created: bool = False,
) -> BaseTapResult:
    return BaseTapResult(
        created,
        receipt.accepted_taps,
        receipt.rejected_taps,
        receipt.earned_points,
        accumulator.accepted_taps,
        max(daily_cap - accumulator.accepted_taps, 0),
        accumulator.pending_points,
        accumulator.settlement_state,
    )


def finalize_tap_accumulator(
    accumulator_id: int, *, at: datetime | None = None, force: bool = False
) -> BaseTapDailyAccumulator:
    """One atomic ledger+projection+drain update. Old worker leases are safely reconciled."""
    now = at or timezone.now()
    if force and settings.BASE_TAP_LEGACY_INGRESS_ENABLED:
        raise ValueError("Pause legacy ingress before forcing a drain")
    identity = BaseTapDailyAccumulator.objects.values("season_id", "player_id").get(
        pk=accumulator_id
    )
    try:
        with transaction.atomic():
            from apps.seasons.barrier import acquire_economic_write_barrier

            acquire_economic_write_barrier(identity["season_id"])
            # Match the ingress/reward lock order. No settlement can race a pending increment.
            player = Player.objects.select_for_update().get(pk=identity["player_id"])
            accumulator = (
                BaseTapDailyAccumulator.objects.select_for_update()
                .select_related("season")
                .get(pk=accumulator_id)
            )
            if accumulator.settlement_state == BaseTapDailyAccumulator.SettlementState.SETTLED:
                return accumulator
            day_closed = now.astimezone(UTC).date() > accumulator.utc_day
            season_closed = accumulator.season.status != Season.Status.ACTIVE
            if not force and not day_closed and not season_closed:
                return accumulator
            config = resolve_game_config_version(accumulator.config_version_id)
            if accumulator.first_accepted_at is None or accumulator.pending_points <= 0:
                raise ValueError("Pending Base Tap accumulator has no accepted economic input")
            settle_player_reward(
                player=player,
                source_type="BASE_TAP",
                source_id=str(accumulator.pk),
                context="CORE_GAMEPLAY",
                amount=accumulator.pending_points,
                idempotency_key=f"base-tap-day:{accumulator.pk}",
                occurred_at=accumulator.first_accepted_at,
                resolved_config=config,
                source_season=accumulator.season,
                preserve_config_version=True,
                create_settlement_outbox=False,
                allow_closed_source_season=True,
                source_eligibility_verified=True,
                projection_at=now,
                metadata={
                    "tap_accumulator_id": accumulator.pk,
                    "utc_day": str(accumulator.utc_day),
                },
            )
            accumulator.settled_points += accumulator.pending_points
            accumulator.pending_points = 0
            accumulator.settlement_state = BaseTapDailyAccumulator.SettlementState.SETTLED
            accumulator.settlement_attempts += 1
            accumulator.lease_until = None
            accumulator.last_error_category = ""
            accumulator.settled_at = now
            accumulator.save()
            return accumulator
    except Exception as exc:
        # Never overwrite a concurrent successful drain with a retry state.
        from django.db.models import F

        BaseTapDailyAccumulator.objects.filter(pk=accumulator_id).exclude(
            settlement_state="SETTLED"
        ).update(
            settlement_state=BaseTapDailyAccumulator.SettlementState.RETRY,
            settlement_attempts=F("settlement_attempts") + 1,
            lease_until=None,
            next_attempt_at=now + timedelta(seconds=settings.BASE_TAP_SETTLEMENT_RETRY_SECONDS),
            last_error_category=type(exc).__name__,
        )
        logger.warning(
            "base_tap_settlement_failed",
            accumulator_id=accumulator_id,
            error_category=type(exc).__name__,
        )
        raise


def recover_pending_tap_accumulators(*, at: datetime | None = None, limit: int = 500) -> int:
    now = at or timezone.now()
    ids = list(
        BaseTapDailyAccumulator.objects.filter(
            settlement_state__in=(
                BaseTapDailyAccumulator.SettlementState.PENDING,
                BaseTapDailyAccumulator.SettlementState.RETRY,
                BaseTapDailyAccumulator.SettlementState.PROCESSING,
            ),
            next_attempt_at__lte=now,
        )
        .order_by("utc_day", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    processed = 0
    for accumulator_id in ids:
        try:
            result = finalize_tap_accumulator(accumulator_id, at=now)
            processed += result.settlement_state == BaseTapDailyAccumulator.SettlementState.SETTLED
        except Exception:
            continue
    return processed


def purge_expired_tap_receipts(*, at: datetime | None = None, limit: int = 1000) -> int:
    ids = list(
        BaseTapReceipt.objects.filter(expires_at__lte=at or timezone.now())
        .order_by("expires_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    deleted, _ = BaseTapReceipt.objects.filter(pk__in=ids).delete()
    return deleted
