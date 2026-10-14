"""Tap v2. Lock order: Season barrier, Player, burst state. Ledger is the receipt."""

from dataclasses import asdict
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any
from uuid import UUID

import structlog
from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Max

from apps.arena.models import PerformanceSourceFact
from apps.base_tap.models import BaseTapDailyAccumulator, TapBurstState, TapLease
from apps.base_tap.schemas import BatchInput
from apps.game_config.services import (
    ResolvedGameConfig,
    resolve_effective_game_config,
    resolve_game_config_version,
)
from apps.players.models import Player
from apps.players.services import reward_gameplay_allowed
from apps.progression.models import PlayerProgression
from apps.progression.services import calculate_progression
from apps.rewards.models import RewardLedger
from apps.rewards.services import settle_player_reward
from apps.seasons.barrier import acquire_economic_write_barrier
from apps.seasons.models import PlayerSeason, Season
from apps.seasons.services import (
    economic_window_end,
    is_reward_earning_time,
    resolve_current_season,
)

logger = structlog.get_logger(__name__)


class TapError(Exception):
    def __init__(
        self,
        code: str,
        status: int = 409,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        super().__init__(code)


def throttle(player: Player, session: UUID, at: datetime) -> None:
    for scope, limit in (
        (f"player:{player.pk}", settings.TAP_V2_PLAYER_REQUESTS),
        (f"session:{player.pk}:{session}", settings.TAP_V2_SESSION_REQUESTS),
    ):
        key = f"tap-v2:{scope}:{int(at.timestamp()) // 60}"
        try:
            count = 1 if cache.add(key, 1, timeout=61) else cache.incr(key)
        except Exception:
            # Burst/sequence correctness is PostgreSQL-owned; cache failures are telemetry.
            logger.warning("tap_throttle_unavailable", player_id=player.pk)
            continue
        if count > limit:
            raise TapError("RATE_LIMITED", 429, retryable=True, retry_after_ms=60000)


def _lease_for(state: TapBurstState | None, at: datetime, session: UUID | None) -> TapLease | None:
    if state is None or session is None:
        return None
    return (
        TapLease.objects.select_related("season")
        .filter(
            player_id=state.player_id,
            generation=state.generation,
            session_id=session,
            revoked_at__isnull=True,
            expires_at__gt=at,
        )
        .first()
    )


def _snapshot(
    player: Player,
    config: ResolvedGameConfig,
    season: Season | None,
    state: TapBurstState | None,
    at: datetime,
    session: UUID | None,
) -> dict[str, Any]:
    projection = PlayerProgression.objects.filter(player=player).first()
    points = projection.lifetime_points if projection else 0
    result = calculate_progression(points, config.progression)
    season_points = (
        PlayerSeason.objects.filter(player=player, season=season)
        .values_list("season_points", flat=True)
        .first()
        if season
        else 0
    )
    same_season = bool(state and season and state.season_id == season.pk)
    lease = _lease_for(state, at, session) if same_season else None
    lease_data = None
    if lease and state:
        bound = (
            config
            if lease.config_version_id == config.id
            else resolve_game_config_version(lease.config_version_id)
        )
        lease_data = {
            "lease_id": lease.pk,
            "session_id": lease.session_id,
            "generation": lease.generation,
            "authorized_allowance": lease.allowance,
            "reward_per_tap": bound.base_tap.points_per_rewarded_tap,
            "cooldown_seconds": bound.base_tap.cooldown_seconds,
            "max_taps_per_batch": bound.base_tap.max_taps_per_batch,
            "min_batch_interval_ms": bound.base_tap.min_batch_interval_ms,
            "expires_at": lease.expires_at,
            "sequence_start": state.next_sequence,
            "sequence_end": state.next_sequence + state.remaining - 1,
            "config_version": bound.version,
            "season_id": lease.season.code,
        }
    cursor = RewardLedger.objects.filter(player=player).aggregate(value=Max("pk"))["value"] or 0
    return {
        "contract_version": "2.0",
        "server_time": at,
        "player_id": str(player.telegram_user_id),
        "season_id": season.code if season else None,
        "config_version": config.version,
        "issuance_enabled": bool(
            settings.TAP_V2_ISSUANCE_ENABLED
            and not settings.BASE_TAP_LEGACY_INGRESS_ENABLED
            and config.base_tap.protocol_version == 2
            and config.base_tap.enabled
        ),
        "reward_earning_allowed": bool(
            season and is_reward_earning_time(season, at) and reward_gameplay_allowed(player)
        ),
        "revision": state.revision if state else 0,
        "generation": state.generation if state else 0,
        "receipt_cursor": cursor,
        "burst_remaining": state.remaining
        if state and season and state.season_id == season.pk
        else 0,
        "cooldown_until": state.cooldown_until if state and same_season else None,
        "lease": lease_data,
        "progression": {"lifetime_points": points, **asdict(result)},
        "season_points": season_points or 0,
        "progression_policy": {
            "levels": [asdict(x) for x in config.progression.levels],
            "dexo_stages": [asdict(x) for x in config.progression.dexo_stages],
            "clan_min_level": config.clan.min_level if config.clan else None,
        },
    }


@transaction.atomic
def state_snapshot(player: Player, *, at: datetime, session: UUID | None = None) -> dict[str, Any]:
    player = Player.objects.select_for_update().get(pk=player.pk)
    config = resolve_effective_game_config(at)
    season = resolve_current_season(at=at, required=False)
    state = TapBurstState.objects.filter(player=player).first()
    return _snapshot(player, config, season, state, at, session)


def _eligible(player: Player, season: Season | None, at: datetime) -> None:
    if not reward_gameplay_allowed(player):
        raise TapError("PLAYER_RESTRICTED", 403)
    if (
        season is None
        or season.status != Season.Status.ACTIVE
        or not is_reward_earning_time(season, at)
    ):
        raise TapError("SEASON_UNAVAILABLE")


def _epoch_guard(season: Season, at: datetime) -> None:
    # Shared row lock permits concurrent players while fencing the exclusive freeze writer.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT state FROM arena_economicepoch WHERE season_id = %s "
            "AND starts_at <= %s AND ends_at > %s FOR SHARE",
            [season.pk, at, at],
        )
        row = cursor.fetchone()
    if row is None or row[0] not in {"OPEN", "DRAINING"}:
        raise TapError("EPOCH_FINALIZED")


def issue_lease(
    player: Player,
    *,
    session: UUID,
    at: datetime,
    takeover: bool = False,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    throttle(player, session, at)
    with transaction.atomic():
        season = resolve_current_season(at=at, required=False)
        if season is None:
            raise TapError("SEASON_UNAVAILABLE")
        acquire_economic_write_barrier(season.pk)
        from apps.arena.epochs import initialize_epochs
        from apps.arena.models import EconomicEpoch

        if not EconomicEpoch.objects.filter(season=season).exists():
            initialize_epochs(season)
        _epoch_guard(season, at)
        player = Player.objects.select_for_update().get(pk=player.pk)
        _eligible(player, season, at)
        config = resolve_effective_game_config(at)
        if (
            not settings.TAP_V2_ISSUANCE_ENABLED
            or settings.BASE_TAP_LEGACY_INGRESS_ENABLED
            or config.base_tap.protocol_version != 2
            or not config.base_tap.enabled
        ):
            raise TapError("ISSUANCE_DISABLED", 503, retryable=True)
        if BaseTapDailyAccumulator.objects.filter(player=player, pending_points__gt=0).exists():
            raise TapError("LEGACY_DRAIN_REQUIRED", 503, retryable=True)
        state, _ = TapBurstState.objects.get_or_create(
            player=player, defaults={"season": season, "config_version_id": config.id}
        )
        active = TapLease.objects.filter(
            player=player, generation=state.generation, revoked_at__isnull=True, expires_at__gt=at
        ).first()
        if active and state.season_id == season.pk and state.remaining:
            if active.session_id == session:
                return _snapshot(player, config, season, state, at, session)
            if not takeover:
                raise TapError("ACTIVE_SESSION")
            if expected_generation != state.generation:
                raise TapError("GENERATION_STALE")
        elif takeover and expected_generation != state.generation:
            raise TapError("GENERATION_STALE")
        if state.cooldown_until and at < state.cooldown_until and state.season_id == season.pk:
            raise TapError(
                "COOLDOWN",
                retryable=True,
                retry_after_ms=max(1, int((state.cooldown_until - at).total_seconds() * 1000)),
            )
        if active:
            active.revoked_at = at
            active.save(update_fields=("revoked_at",))
        if state.season_id != season.pk or state.remaining == 0:
            state.season = season
            state.config_version_id = config.id
            state.remaining = config.base_tap.rewarded_taps_per_burst
            state.next_sequence = 1
            state.cooldown_until = None
            state.last_batch_at = None
        # Interrupted bursts keep their original immutable policy, including cooldown.
        bound = (
            config
            if state.config_version_id == config.id
            else resolve_game_config_version(state.config_version_id)
        )
        epoch_days = bound.dlx_distribution.epoch_days if bound.dlx_distribution else 7
        epoch_end = season.starts_at + timedelta(
            days=((at - season.starts_at).days // epoch_days + 1) * epoch_days
        )
        expires = min(
            at + timedelta(seconds=bound.base_tap.lease_seconds),
            economic_window_end(season),
            epoch_end,
        )
        # A scheduled publication never invalidates an existing burst. The lease remains pinned.
        state.generation += 1
        state.revision += 1
        state.save()
        TapLease.objects.create(
            player=player,
            season=season,
            config_version_id=bound.id,
            session_id=session,
            generation=state.generation,
            allowance=state.remaining,
            issued_at=at,
            expires_at=expires,
            delete_after=expires + timedelta(days=bound.base_tap.receipt_retention_days),
        )
        logger.info(
            "tap_lease_issued", player_id=player.pk, generation=state.generation, takeover=takeover
        )
        return _snapshot(player, config, season, state, at, session)


def _request_identity(payload: BatchInput) -> dict[str, Any]:
    return payload.model_dump(mode="json", exclude={"idempotency_key"})


def _receipt_evidence(ledger: RewardLedger) -> dict[str, Any]:
    request = ledger.metadata["tap_request"]
    return {
        "receipt_id": ledger.pk,
        "idempotency_key": ledger.idempotency_key.removeprefix(f"tap-v2:{ledger.player_id}:"),
        "lease_id": request["lease_id"],
        "session_id": request["session_id"],
        "generation": request["generation"],
        "sequence_start": request["sequence_start"],
        "requested_taps": request["tap_count"],
        **ledger.metadata["tap_receipt"],
        "accepted_at": ledger.occurred_at,
        "season_id": ledger.season.code,
        "config_version": ledger.config_version.version,
    }


def submit_batch(player: Player, payload: BatchInput, *, at: datetime) -> dict[str, Any]:
    started = perf_counter()
    throttle(player, payload.session_id, at)
    key = f"tap-v2:{player.pk}:{payload.idempotency_key}"
    with transaction.atomic():
        # Look up durable receipt before expiry/rollover/moderation; settled value always survives.
        existing = (
            RewardLedger.objects.select_related("season", "config_version")
            .filter(idempotency_key=key)
            .first()
        )
        lease = (
            TapLease.objects.select_related("season")
            .filter(pk=payload.lease_id, player=player)
            .first()
            if not existing
            else None
        )
        season_id = existing.season_id if existing else lease.season_id if lease else None
        if season_id is None:
            raise TapError("LEASE_UNKNOWN", 404)
        if not existing:
            acquire_economic_write_barrier(season_id)
            assert lease is not None
            _epoch_guard(lease.season, at)
        lock_started = perf_counter()
        player = Player.objects.select_for_update().get(pk=player.pk)
        lock_ms = (perf_counter() - lock_started) * 1000
        state = TapBurstState.objects.filter(player=player).first()
        existing = (
            RewardLedger.objects.select_related("season", "config_version")
            .filter(idempotency_key=key)
            .first()
        )
        current = resolve_effective_game_config(at)
        current_season = resolve_current_season(at=at, required=False)
        if existing:
            if existing.metadata.get("tap_request") != _request_identity(payload):
                raise TapError("IDEMPOTENCY_CONFLICT")
            receipt = existing.metadata["tap_receipt"]
            return {
                **receipt,
                "duplicate": True,
                "receipt_id": existing.pk,
                "receipt": _receipt_evidence(existing),
                "snapshot": _snapshot(
                    player, current, current_season, state, at, payload.session_id
                ),
            }
        assert lease is not None
        _eligible(player, lease.season, at)
        if current_season is None or current_season.pk != lease.season_id:
            raise TapError("SEASON_CHANGED")
        if (
            lease.revoked_at
            or state is None
            or state.generation != payload.generation
            or lease.generation != payload.generation
            or lease.session_id != payload.session_id
            or state.season_id != lease.season_id
        ):
            raise TapError("LEASE_REVOKED")
        if at >= lease.expires_at:
            raise TapError("LEASE_EXPIRED")
        bound = (
            current
            if lease.config_version_id == current.id
            else resolve_game_config_version(lease.config_version_id)
        )
        policy = bound.base_tap
        if payload.tap_count > policy.max_taps_per_batch:
            raise TapError("BATCH_TOO_LARGE", 422)
        if payload.sequence_start != state.next_sequence:
            raise TapError(
                "SEQUENCE_GAP"
                if payload.sequence_start > state.next_sequence
                else "SEQUENCE_REPLAY",
                retryable=payload.sequence_start > state.next_sequence,
            )
        if not state.remaining:
            raise TapError("ALLOWANCE_EXHAUSTED")
        if (
            state.last_batch_at
            and (at - state.last_batch_at).total_seconds() * 1000 < policy.min_batch_interval_ms
        ):
            raise TapError(
                "RATE_LIMITED", 429, retryable=True, retry_after_ms=policy.min_batch_interval_ms
            )
        accepted = min(payload.tap_count, state.remaining)
        earned = accepted * policy.points_per_rewarded_tap
        receipt = {
            "accepted_taps": accepted,
            "rejected_taps": payload.tap_count - accepted,
            "earned_points": earned,
            "sequence_through": state.next_sequence + accepted - 1,
        }
        ledger, _ = settle_player_reward(
            player=player,
            source_type="BASE_TAP",
            source_id=f"v2:{lease.pk}:{payload.sequence_start}",
            context="CORE_GAMEPLAY",
            amount=earned,
            idempotency_key=key,
            occurred_at=at,
            resolved_config=bound,
            source_season=lease.season,
            preserve_config_version=True,
            create_settlement_outbox=False,
            metadata={
                "tap_protocol": 2,
                "tap_request": _request_identity(payload),
                "tap_receipt": receipt,
            },
            projection_at=at if bound.id != current.id else None,
        )
        PerformanceSourceFact.objects.create(
            season=lease.season,
            player=player,
            category="CORE_GAMEPLAY",
            source_type="BASE_TAP_V2_BATCH",
            source_id=str(ledger.pk),
            source_time=at,
            amount=earned,
            config_version_id=bound.id,
            metadata={
                "lease_id": str(lease.pk),
                "sequence_start": payload.sequence_start,
                "accepted_taps": accepted,
            },
        )
        state.remaining -= accepted
        state.next_sequence += accepted
        state.last_batch_at = at
        state.revision += 1
        if state.remaining == 0:
            state.cooldown_until = at + timedelta(seconds=policy.cooldown_seconds)
        state.save()
        result = {
            **receipt,
            "duplicate": False,
            "receipt_id": ledger.pk,
            "receipt": _receipt_evidence(ledger),
            "snapshot": _snapshot(player, current, current_season, state, at, payload.session_id),
        }
    logger.info(
        "tap_batch_committed",
        player_id=player.pk,
        accepted_taps=accepted,
        batch_size=payload.tap_count,
        player_lock_ms=round(lock_ms, 3),
        transaction_ms=round((perf_counter() - started) * 1000, 3),
        receipt_id=ledger.pk,
    )
    return result


def purge_expired_leases(*, at: datetime, limit: int = 1000) -> int:
    ids = list(
        TapLease.objects.filter(delete_after__lte=at)
        .order_by("delete_after")
        .values_list("pk", flat=True)[:limit]
    )
    deleted, _ = TapLease.objects.filter(pk__in=ids).delete()
    return deleted
