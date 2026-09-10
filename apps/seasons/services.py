from datetime import datetime

import structlog
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_game_config_version
from apps.seasons.models import Season, SeasonSettlementControlEvent

logger = structlog.get_logger(__name__)


class NoActiveSeason(LookupError):
    pass


class SeasonNotEligible(ValueError):
    pass


def resolve_current_season(*, at: datetime, required: bool = True) -> Season | None:
    active = list(Season.objects.filter(status=Season.Status.ACTIVE)[:2])
    if len(active) > 1:
        raise ValidationError("More than one active season exists")
    if not active:
        if required:
            raise NoActiveSeason("No active season")
        return None
    season = active[0]
    if not (season.starts_at <= at < season.ends_at):
        if required:
            raise SeasonNotEligible("Active season is outside its settlement window")
        return None
    return season


def resolve_historical_season(*, at: datetime, required: bool = True) -> Season | None:
    seasons = list(
        Season.objects.filter(
            starts_at__lte=at,
            ends_at__gt=at,
            status__in=(Season.Status.ACTIVE, Season.Status.CLOSED, Season.Status.ARCHIVED),
        ).order_by("pk")[:2]
    )
    if len(seasons) > 1:
        raise ValidationError("More than one historical Season contains the timestamp")
    if not seasons:
        if required:
            raise NoActiveSeason("No historical Season contains the timestamp")
        return None
    return seasons[0]


def activate_season(season: Season) -> Season:
    with transaction.atomic():
        season = (
            Season.objects.select_for_update().select_related("baseline_config").get(pk=season.pk)
        )
        if season.status != Season.Status.DRAFT or season.ends_at <= season.starts_at:
            raise ValidationError("Only a valid draft season can be activated")
        if season.baseline_config.status != GameConfigVersion.Status.PUBLISHED:
            raise ValidationError("Season baseline configuration must be published")
        if (
            Season.objects.select_for_update()
            .filter(status=Season.Status.ACTIVE)
            .exclude(pk=season.pk)
            .exists()
        ):
            raise ValidationError("An active season already exists")
        season.status = Season.Status.ACTIVE
        season.save(update_fields=["status", "updated_at"])
    logger.info("season_activated", season_id=season.pk, season_code=season.code)
    return season


def set_settlement_start_epoch(season: Season, epoch: int) -> Season:
    """Change the audited operational gate only before any settlement has begun."""
    with transaction.atomic():
        season = Season.objects.select_for_update().get(pk=season.pk)
        config = resolve_game_config_version(season.baseline_config_id)
        distribution = config.dlx_distribution
        if (
            distribution is None
            or type(epoch) is not int
            or not 1 <= epoch <= distribution.epoch_count
        ):
            raise ValidationError("Settlement start epoch is outside the configured epoch range")
        if season.settlement_started_at is not None:
            raise ValidationError("Settlement history has begun and cannot be rewritten")
        previous = season.settlement_start_epoch
        season.settlement_start_epoch = epoch
        season.save(update_fields=("settlement_start_epoch", "updated_at"))
        SeasonSettlementControlEvent.objects.create(
            season=season, previous_start_epoch=previous, new_start_epoch=epoch
        )
    logger.info("settlement_start_epoch_changed", season_id=season.pk, epoch=epoch)
    return season
