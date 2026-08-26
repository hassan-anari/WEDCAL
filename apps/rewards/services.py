from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog
from django.db import IntegrityError, transaction
from django.db.models import Sum

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ResolvedGameConfig, resolve_effective_game_config
from apps.players.models import Player
from apps.progression.models import PlayerProgression
from apps.progression.services import calculate_progression
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
from apps.seasons.models import PlayerSeason, Season
from apps.seasons.services import resolve_current_season

logger = structlog.get_logger(__name__)


class InvalidReward(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


def _matches(
    entry: RewardLedger,
    *,
    player: Player,
    season_id: int,
    source_type: str,
    source_id: str,
    context: str,
    amount: int,
    config_id: int,
) -> bool:
    return (
        entry.player_id == player.pk
        and entry.season_id == season_id
        and entry.source_type == source_type
        and entry.source_id == source_id
        and entry.context == context
        and entry.gross_amount == amount
        and entry.player_amount == amount
        and entry.config_version_id == config_id
    )


def settle_player_reward(
    *,
    player: Player,
    source_type: str,
    source_id: str,
    context: str,
    amount: int,
    idempotency_key: str,
    occurred_at: datetime,
    resolved_config: ResolvedGameConfig,
    metadata: dict[str, Any] | None = None,
    after_ledger_hook: Callable[[], None] | None = None,
    source_season: Season | None = None,
    preserve_config_version: bool = False,
) -> tuple[RewardLedger, bool]:
    if type(amount) is not int or amount <= 0:
        raise InvalidReward("Player reward amount must be a positive integer")
    with transaction.atomic():
        locked_player = Player.objects.select_for_update().get(pk=player.pk)
        season = source_season or resolve_current_season(at=occurred_at)
        assert season is not None
        if season.status != Season.Status.ACTIVE:
            raise InvalidReward("Source Season is not active")
        effective = resolve_effective_game_config(occurred_at)
        if not preserve_config_version and effective.id != resolved_config.id:
            raise InvalidReward("Supplied configuration is not effective for occurred_at")
        existing = RewardLedger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            if not _matches(
                existing,
                player=locked_player,
                season_id=season.pk,
                source_type=source_type,
                source_id=source_id,
                context=context,
                amount=amount,
                config_id=resolved_config.id,
            ):
                logger.warning(
                    "reward_idempotency_conflict", player_id=player.pk, source_type=source_type
                )
                raise IdempotencyConflict("Idempotency key belongs to a different settlement")
            logger.info("reward_duplicate", player_id=player.pk, reward_id=existing.pk)
            return existing, False
        config_model = GameConfigVersion.objects.get(pk=resolved_config.id)
        try:
            with transaction.atomic():
                ledger = RewardLedger.objects.create(
                    player=locked_player,
                    season=season,
                    source_type=source_type,
                    source_id=source_id,
                    context=context,
                    gross_amount=amount,
                    player_amount=amount,
                    clan_treasury_amount=0,
                    referral_amount=0,
                    config_version=config_model,
                    idempotency_key=idempotency_key,
                    metadata=metadata or {},
                    occurred_at=occurred_at,
                )
                outbox = RewardSettlementOutbox.objects.create(reward=ledger)
        except IntegrityError:
            existing = RewardLedger.objects.filter(idempotency_key=idempotency_key).first()
            if existing is None:
                raise
            if not _matches(
                existing,
                player=locked_player,
                season_id=season.pk,
                source_type=source_type,
                source_id=source_id,
                context=context,
                amount=amount,
                config_id=resolved_config.id,
            ):
                logger.warning(
                    "reward_idempotency_conflict", player_id=player.pk, source_type=source_type
                )
                raise IdempotencyConflict(
                    "Idempotency key belongs to a different settlement"
                ) from None
            logger.info("reward_duplicate", player_id=player.pk, reward_id=existing.pk)
            return existing, False
        if after_ledger_hook:
            after_ledger_hook()
        progression, _ = PlayerProgression.objects.get_or_create(
            player=locked_player,
            defaults={
                "config_version": config_model,
                "dexo_stage": resolved_config.progression.dexo_stages[0].key,
            },
        )
        progression.lifetime_points += amount
        result = calculate_progression(progression.lifetime_points, resolved_config.progression)
        progression.level = result.level
        progression.dexo_stage = result.dexo_stage
        progression.config_version = config_model
        progression.save(
            update_fields=["lifetime_points", "level", "dexo_stage", "config_version", "updated_at"]
        )
        player_season, _ = PlayerSeason.objects.get_or_create(player=locked_player, season=season)
        player_season.season_points += amount
        player_season.save(update_fields=["season_points", "updated_at"])
        transaction.on_commit(lambda: dispatch_reward_outbox_safely(outbox.pk))
    logger.info(
        "reward_settled",
        player_id=player.pk,
        reward_id=ledger.pk,
        season_id=season.pk,
        source_type=source_type,
    )
    return ledger, True


def dispatch_reward_outbox_safely(outbox_id: int) -> None:
    try:
        from apps.rewards.tasks import process_reward_outbox

        process_reward_outbox.delay(outbox_id)
    except Exception as exc:
        logger.warning(
            "reward_outbox_dispatch_failed", outbox_id=outbox_id, error_category=type(exc).__name__
        )


def rebuild_player_reward_projections(
    player: Player, resolved_config: ResolvedGameConfig
) -> PlayerProgression:
    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player.pk)
        config_model = GameConfigVersion.objects.get(pk=resolved_config.id)
        lifetime = (
            RewardLedger.objects.filter(player=player).aggregate(total=Sum("player_amount"))[
                "total"
            ]
            or 0
        )
        if lifetime < 0:
            raise InvalidReward("Ledger would create a negative projection")
        projection, _ = PlayerProgression.objects.get_or_create(
            player=player,
            defaults={
                "config_version": config_model,
                "dexo_stage": resolved_config.progression.dexo_stages[0].key,
            },
        )
        result = calculate_progression(lifetime, resolved_config.progression)
        projection.lifetime_points = lifetime
        projection.level = result.level
        projection.dexo_stage = result.dexo_stage
        projection.config_version = config_model
        projection.save()
        totals = (
            RewardLedger.objects.filter(player=player)
            .values("season_id")
            .annotate(total=Sum("player_amount"))
        )
        seen: set[int] = set()
        for row in totals:
            season_id = int(row["season_id"])
            total = int(row["total"])
            if total < 0:
                raise InvalidReward("Ledger would create a negative season projection")
            PlayerSeason.objects.update_or_create(
                player=player, season_id=season_id, defaults={"season_points": total}
            )
            seen.add(season_id)
        PlayerSeason.objects.filter(player=player).exclude(season_id__in=seen).update(
            season_points=0
        )
    logger.info("reward_projection_rebuilt", player_id=player.pk)
    return projection
