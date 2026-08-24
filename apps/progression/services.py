from dataclasses import dataclass

import structlog
from django.db import transaction

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ProgressionConfig, ResolvedGameConfig
from apps.players.models import Player
from apps.progression.models import PlayerProgression

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProgressionResult:
    level: int
    current_threshold: int
    next_threshold: int | None
    points_into_level: int
    points_remaining: int | None
    dexo_stage: str


def calculate_progression(points: int, config: ProgressionConfig) -> ProgressionResult:
    if type(points) is not int or points < 0:
        raise ValueError("Lifetime points must be a nonnegative integer")
    current = config.levels[0]
    for rule in config.levels:
        if rule.min_lifetime_points > points:
            break
        current = rule
    next_rule = next((x for x in config.levels if x.level > current.level), None)
    stage = config.dexo_stages[0]
    for candidate in config.dexo_stages:
        if candidate.min_level > current.level:
            break
        stage = candidate
    return ProgressionResult(
        level=current.level,
        current_threshold=current.min_lifetime_points,
        next_threshold=next_rule.min_lifetime_points if next_rule else None,
        points_into_level=points - current.min_lifetime_points,
        points_remaining=(next_rule.min_lifetime_points - points) if next_rule else None,
        dexo_stage=stage.key,
    )


def refresh_player_progression(
    player: Player, resolved: ResolvedGameConfig, *, force: bool = False
) -> tuple[PlayerProgression, ProgressionResult]:
    with transaction.atomic():
        locked_player = Player.objects.select_for_update().get(pk=player.pk)
        config_model = GameConfigVersion.objects.get(pk=resolved.id)
        projection, _ = PlayerProgression.objects.get_or_create(
            player=locked_player,
            defaults={
                "config_version": config_model,
                "dexo_stage": resolved.progression.dexo_stages[0].key,
            },
        )
        projection = PlayerProgression.objects.select_for_update().get(pk=projection.pk)
        result = calculate_progression(projection.lifetime_points, resolved.progression)
        if (
            force
            or projection.config_version_id != resolved.id
            or projection.level != result.level
            or projection.dexo_stage != result.dexo_stage
        ):
            projection.level = result.level
            projection.dexo_stage = result.dexo_stage
            projection.config_version = config_model
            projection.save(update_fields=["level", "dexo_stage", "config_version", "updated_at"])
            logger.info(
                "progression_projection_refreshed",
                player_id=player.pk,
                config_version=resolved.version,
            )
    return projection, result
