import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.game_config.models import GameConfigVersion

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LevelRule:
    level: int
    min_lifetime_points: int


@dataclass(frozen=True, slots=True)
class DexoStageRule:
    key: str
    min_level: int


@dataclass(frozen=True, slots=True)
class ProgressionConfig:
    levels: tuple[LevelRule, ...]
    dexo_stages: tuple[DexoStageRule, ...]


@dataclass(frozen=True, slots=True)
class ResolvedGameConfig:
    id: int
    version: str
    effective_at: datetime
    progression: ProgressionConfig


class GameConfigUnavailable(LookupError):
    pass


def parse_progression(payload: dict[str, Any]) -> ProgressionConfig:
    try:
        raw = payload["progression"]
        raw_levels = raw["levels"]
        raw_stages = raw["dexo_stages"]
        if not isinstance(raw_levels, list) or not isinstance(raw_stages, list):
            raise TypeError
        levels = tuple(LevelRule(x["level"], x["min_lifetime_points"]) for x in raw_levels)
        stages = tuple(DexoStageRule(x["key"], x["min_level"]) for x in raw_stages)
    except (KeyError, TypeError) as exc:
        raise ValidationError("Invalid progression configuration shape") from exc
    if not levels or any(
        type(x.level) is not int or type(x.min_lifetime_points) is not int for x in levels
    ):
        raise ValidationError("Level values and thresholds must be integers")
    if levels[0] != LevelRule(1, 0):
        raise ValidationError("Level 1 must start at zero")
    if len({x.level for x in levels}) != len(levels) or any(
        x.min_lifetime_points < 0 for x in levels
    ):
        raise ValidationError("Levels must be unique with nonnegative thresholds")
    if list(levels) != sorted(levels, key=lambda x: (x.level, x.min_lifetime_points)):
        raise ValidationError("Levels must be deterministically ordered")
    if any(
        b.level <= a.level or b.min_lifetime_points <= a.min_lifetime_points
        for a, b in zip(levels, levels[1:], strict=False)
    ):
        raise ValidationError("Level thresholds must strictly ascend")
    if not stages or any(
        type(x.min_level) is not int or not isinstance(x.key, str) or not x.key for x in stages
    ):
        raise ValidationError("Dexo stages are invalid")
    if stages[0].min_level != 1 or len({x.key for x in stages}) != len(stages):
        raise ValidationError("Dexo stages require a unique level-one baseline")
    if any(b.min_level <= a.min_level for a, b in zip(stages, stages[1:], strict=False)):
        raise ValidationError("Dexo stage thresholds must strictly ascend")
    return ProgressionConfig(levels, stages)


def publish_game_config(
    config: GameConfigVersion, *, published_at: datetime | None = None
) -> GameConfigVersion:
    with transaction.atomic():
        config = GameConfigVersion.objects.select_for_update().get(pk=config.pk)
        if config.status != GameConfigVersion.Status.DRAFT:
            raise ValidationError("Only draft configurations can be published")
        parse_progression(config.payload)
        if GameConfigVersion.objects.filter(
            status=GameConfigVersion.Status.PUBLISHED, effective_at__gte=config.effective_at
        ).exists():
            raise ValidationError(
                "Publishing would rewrite or ambiguate established config history"
            )
        canonical = json.dumps(config.payload, sort_keys=True, separators=(",", ":")).encode()
        config.payload_checksum = hashlib.sha256(canonical).hexdigest()
        config.status = GameConfigVersion.Status.PUBLISHED
        config.published_at = published_at or timezone.now()
        config.save(update_fields=["payload_checksum", "status", "published_at", "updated_at"])
    logger.info("game_config_published", config_version=config.version)
    return config


def resolve_effective_game_config(at: datetime) -> ResolvedGameConfig:
    configs = list(
        GameConfigVersion.objects.filter(
            status=GameConfigVersion.Status.PUBLISHED, effective_at__lte=at
        ).order_by("-effective_at", "-pk")[:2]
    )
    if not configs:
        raise GameConfigUnavailable("No published game configuration is effective")
    config = configs[0]
    return ResolvedGameConfig(
        config.pk, config.version, config.effective_at, parse_progression(config.payload)
    )
