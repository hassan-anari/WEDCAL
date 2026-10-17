from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog
from django.db import IntegrityError, transaction
from django.db.models import F, Max, Sum
from django.utils import timezone

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ResolvedGameConfig, resolve_effective_game_config
from apps.players.models import Player
from apps.players.services import reward_gameplay_allowed
from apps.progression.models import PlayerProgression
from apps.progression.services import calculate_progression
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
from apps.seasons.models import PlayerSeason, Season
from apps.seasons.services import require_reward_earning_time, resolve_current_season

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
    gross_amount: int,
    player_amount: int,
    clan_treasury_amount: int,
    config_id: int,
) -> bool:
    return (
        entry.player_id == player.pk
        and entry.season_id == season_id
        and entry.source_type == source_type
        and entry.source_id == source_id
        and entry.context == context
        and entry.gross_amount == gross_amount
        and entry.player_amount == player_amount
        and entry.clan_treasury_amount == clan_treasury_amount
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
    create_settlement_outbox: bool = True,
    allow_closed_source_season: bool = False,
    projection_at: datetime | None = None,
    source_eligibility_verified: bool = False,
) -> tuple[RewardLedger, bool]:
    return settle_reward(
        player=player,
        source_type=source_type,
        source_id=source_id,
        context=context,
        gross_amount=amount,
        player_amount=amount,
        clan_treasury_amount=0,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at,
        resolved_config=resolved_config,
        metadata=metadata,
        after_ledger_hook=after_ledger_hook,
        source_season=source_season,
        preserve_config_version=preserve_config_version,
        create_settlement_outbox=create_settlement_outbox,
        allow_closed_source_season=allow_closed_source_season,
        projection_at=projection_at,
        source_eligibility_verified=source_eligibility_verified,
    )


def settle_reward(
    *,
    player: Player,
    source_type: str,
    source_id: str,
    context: str,
    gross_amount: int,
    player_amount: int,
    clan_treasury_amount: int,
    idempotency_key: str,
    occurred_at: datetime,
    resolved_config: ResolvedGameConfig,
    metadata: dict[str, Any] | None = None,
    after_ledger_hook: Callable[[], None] | None = None,
    source_season: Season | None = None,
    preserve_config_version: bool = False,
    create_settlement_outbox: bool = True,
    allow_closed_source_season: bool = False,
    projection_at: datetime | None = None,
    source_eligibility_verified: bool = False,
    allow_treasury_only_split: bool = False,
) -> tuple[RewardLedger, bool]:
    """Canonical reward primitive; callers own related domain mutations in this transaction."""
    amount = player_amount
    treasury_only = (
        allow_treasury_only_split
        and type(amount) is int
        and amount == 0
        and type(gross_amount) is int
        and gross_amount > 0
        and type(clan_treasury_amount) is int
        and clan_treasury_amount == gross_amount
        and context == "CLAN_HOME"
        and source_type in {"CLAN_ACTIVITY", "CLAN_MISSION"}
    )
    if type(amount) is not int or (amount <= 0 and not treasury_only):
        raise InvalidReward("Player reward amount must be a positive integer")
    if any(type(x) is not int or x < 0 for x in (gross_amount, clan_treasury_amount)):
        raise InvalidReward("Reward split amounts must be nonnegative integers")
    if gross_amount != player_amount + clan_treasury_amount:
        raise InvalidReward("Reward split must reconcile to gross amount")
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier

        season = source_season or resolve_current_season(at=occurred_at)
        assert season is not None
        existing = RewardLedger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            if not _matches(
                existing,
                player=player,
                season_id=season.pk,
                source_type=source_type,
                source_id=source_id,
                context=context,
                gross_amount=gross_amount,
                player_amount=player_amount,
                clan_treasury_amount=clan_treasury_amount,
                config_id=resolved_config.id,
            ):
                raise IdempotencyConflict("Idempotency key belongs to another reward")
            return existing, False
        acquire_economic_write_barrier(season.pk)
        locked_player = Player.objects.select_for_update().get(pk=player.pk)
        if season.status != Season.Status.ACTIVE and not (
            allow_closed_source_season and season.status == Season.Status.CLOSED
        ):
            raise InvalidReward("Source Season is not active")
        require_reward_earning_time(season, occurred_at)
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
                gross_amount=gross_amount,
                player_amount=player_amount,
                clan_treasury_amount=clan_treasury_amount,
                config_id=resolved_config.id,
            ):
                logger.warning(
                    "reward_idempotency_conflict", player_id=player.pk, source_type=source_type
                )
                raise IdempotencyConflict("Idempotency key belongs to a different settlement")
            logger.info("reward_duplicate", player_id=player.pk, reward_id=existing.pk)
            return existing, False
        if not source_eligibility_verified and not reward_gameplay_allowed(locked_player):
            raise InvalidReward("Player moderation state blocks reward generation")
        projection_config = (
            resolve_effective_game_config(projection_at or timezone.now())
            if allow_closed_source_season or projection_at is not None
            else resolved_config
        )
        config_model = GameConfigVersion.objects.get(pk=resolved_config.id)
        projection_config_model = GameConfigVersion.objects.get(pk=projection_config.id)
        try:
            with transaction.atomic():
                ledger = RewardLedger.objects.create(
                    player=locked_player,
                    season=season,
                    source_type=source_type,
                    source_id=source_id,
                    context=context,
                    gross_amount=gross_amount,
                    player_amount=player_amount,
                    clan_treasury_amount=clan_treasury_amount,
                    referral_amount=0,
                    config_version=config_model,
                    idempotency_key=idempotency_key,
                    metadata=metadata or {},
                    occurred_at=occurred_at,
                )
                outbox = (
                    RewardSettlementOutbox.objects.create(reward=ledger)
                    if create_settlement_outbox
                    else None
                )
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
                gross_amount=gross_amount,
                player_amount=player_amount,
                clan_treasury_amount=clan_treasury_amount,
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
        # A fully conserved Clan Treasury rollover still records provenance/outbox,
        # but cannot move a Player's score, tie timestamp, generation or progression.
        if amount:
            progression, _ = PlayerProgression.objects.get_or_create(
                player=locked_player,
                defaults={
                    "config_version": projection_config_model,
                    "dexo_stage": projection_config.progression.dexo_stages[0].key,
                },
            )
            progression.lifetime_points += amount
            result = calculate_progression(
                progression.lifetime_points, projection_config.progression
            )
            progression.level = result.level
            progression.dexo_stage = result.dexo_stage
            progression.config_version = projection_config_model
            progression.save(
                update_fields=[
                    "lifetime_points",
                    "level",
                    "dexo_stage",
                    "config_version",
                    "updated_at",
                ]
            )
            player_season, _ = PlayerSeason.objects.get_or_create(
                player=locked_player, season=season
            )
            player_season.season_points += amount
            player_season.score_reached_at = max(
                filter(None, (player_season.score_reached_at, occurred_at))
            )
            player_season.ranking_generation += 1
            player_season.save(
                update_fields=[
                    "season_points",
                    "score_reached_at",
                    "ranking_generation",
                    "updated_at",
                ]
            )
            from apps.arena.services import publish_player_ranking

            publish_player_ranking(player_season, source_id=f"reward:{ledger.pk}")
        if outbox is not None:
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
            .annotate(total=Sum("player_amount"), reached_at=Max("occurred_at"))
        )
        seen: set[int] = set()
        for row in totals:
            season_id = int(row["season_id"])
            total = int(row["total"])
            if total < 0:
                raise InvalidReward("Ledger would create a negative season projection")
            current, _ = PlayerSeason.objects.get_or_create(player=player, season_id=season_id)
            changed = (
                current.season_points != total or current.score_reached_at != row["reached_at"]
            )
            current.season_points = total
            current.score_reached_at = row["reached_at"]
            if changed:
                current.ranking_generation += 1
            current.save()
            seen.add(season_id)
        PlayerSeason.objects.filter(player=player).exclude(season_id__in=seen).exclude(
            season_points=0, score_reached_at__isnull=True
        ).update(
            season_points=0,
            score_reached_at=None,
            ranking_generation=F("ranking_generation") + 1,
        )
    logger.info("reward_projection_rebuilt", player_id=player.pk)
    return projection
