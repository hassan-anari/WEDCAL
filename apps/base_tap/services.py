from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from django.db import transaction
from django.utils import timezone

from apps.base_tap.models import BaseTapBatch, BaseTapDailyAccumulator
from apps.game_config.services import resolve_effective_game_config, resolve_game_config_version
from apps.players.models import Player
from apps.rewards.services import settle_player_reward
from apps.seasons.services import resolve_current_season

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BaseTapResult:
    batch: BaseTapBatch
    created: bool
    daily_accepted_taps: int
    daily_remaining_taps: int
    pending_points: int


def submit_tap_batch(
    player: Player, *, tap_count: int, idempotency_key: str, at: datetime
) -> BaseTapResult:
    if not idempotency_key or len(idempotency_key) > 100:
        raise ValueError("A bounded Idempotency-Key is required")
    if type(tap_count) is not int or tap_count <= 0:
        raise ValueError("tap_count must be a positive integer")
    config = resolve_effective_game_config(at)
    if not config.base_tap.enabled:
        raise LookupError("Base Tap is unavailable")
    season = resolve_current_season(at=at)
    assert season is not None
    utc_day = at.astimezone(UTC).date()
    with transaction.atomic():
        locked_player = Player.objects.select_for_update().get(pk=player.pk)
        existing = (
            BaseTapBatch.objects.select_related("accumulator")
            .filter(player=locked_player, idempotency_key=idempotency_key)
            .first()
        )
        if existing:
            if existing.requested_taps != tap_count:
                raise ValueError("Idempotency-Key was used with a different tap count")
            return _result(existing, False, config.base_tap.rewarded_taps_per_day)
        accumulator, _ = BaseTapDailyAccumulator.objects.select_for_update().get_or_create(
            player=locked_player,
            season=season,
            utc_day=utc_day,
        )
        rate_limited = bool(
            accumulator.last_batch_at
            and (at - accumulator.last_batch_at).total_seconds() * 1000
            < config.base_tap.min_batch_interval_ms
        )
        available = max(config.base_tap.rewarded_taps_per_day - accumulator.accepted_taps, 0)
        accepted = (
            0 if rate_limited else min(tap_count, config.base_tap.max_taps_per_batch, available)
        )
        batch = BaseTapBatch.objects.create(
            player=locked_player,
            accumulator=accumulator,
            config_version_id=config.id,
            idempotency_key=idempotency_key,
            requested_taps=tap_count,
            accepted_taps=accepted,
            rejected_taps=tap_count - accepted,
            earned_points=accepted * config.base_tap.points_per_rewarded_tap,
            accepted_at=at,
        )
        accumulator.accepted_taps += accepted
        accumulator.last_batch_at = at
        accumulator.save(update_fields=("accepted_taps", "last_batch_at", "updated_at"))
    if batch.earned_points:
        try:
            finalize_tap_batch(batch.pk, at=at)
        except Exception as exc:
            logger.warning(
                "base_tap_settlement_deferred",
                batch_id=batch.pk,
                player_id=player.pk,
                error_category=type(exc).__name__,
            )
    batch.refresh_from_db()
    return _result(batch, True, config.base_tap.rewarded_taps_per_day)


def _result(batch: BaseTapBatch, created: bool, daily_cap: int) -> BaseTapResult:
    accumulator = batch.accumulator
    accumulator.refresh_from_db()
    pending = sum(
        accumulator.batches.filter(status=BaseTapBatch.Status.PENDING).values_list(
            "earned_points", flat=True
        )
    )
    return BaseTapResult(
        batch,
        created,
        accumulator.accepted_taps,
        max(daily_cap - accumulator.accepted_taps, 0),
        pending,
    )


def finalize_tap_batch(batch_id: int, *, at: datetime | None = None) -> BaseTapBatch:
    with transaction.atomic():
        batch = (
            BaseTapBatch.objects.select_for_update()
            .select_related("player", "accumulator__season", "config_version")
            .get(pk=batch_id)
        )
        if batch.status == BaseTapBatch.Status.SETTLED or not batch.earned_points:
            if not batch.earned_points and batch.status != BaseTapBatch.Status.SETTLED:
                batch.status = BaseTapBatch.Status.SETTLED
                batch.settled_at = at or timezone.now()
                batch.save(update_fields=("status", "settled_at"))
            return batch
        config = resolve_game_config_version(batch.config_version_id)
        settle_player_reward(
            player=batch.player,
            source_type="BASE_TAP",
            source_id=str(batch.pk),
            context="CORE_GAMEPLAY",
            amount=batch.earned_points,
            idempotency_key=f"base-tap:{batch.pk}",
            occurred_at=batch.accepted_at,
            resolved_config=config,
            source_season=batch.accumulator.season,
            preserve_config_version=True,
            create_settlement_outbox=False,
            allow_closed_source_season=True,
            metadata={"tap_batch_id": batch.pk, "utc_day": str(batch.accumulator.utc_day)},
        )
        batch.status = BaseTapBatch.Status.SETTLED
        batch.settled_at = at or timezone.now()
        batch.save(update_fields=("status", "settled_at"))
        accumulator = BaseTapDailyAccumulator.objects.select_for_update().get(
            pk=batch.accumulator_id
        )
        accumulator.settled_points += batch.earned_points
        accumulator.save(update_fields=("settled_points", "updated_at"))
    return batch


def recover_pending_tap_batches(*, limit: int = 500) -> int:
    ids = list(
        BaseTapBatch.objects.filter(status=BaseTapBatch.Status.PENDING, earned_points__gt=0)
        .order_by("accepted_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    for batch_id in ids:
        finalize_tap_batch(batch_id)
    return len(ids)
