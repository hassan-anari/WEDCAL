import uuid
from datetime import datetime, timedelta
from typing import Any

import structlog
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_game_config_version
from apps.seasons.models import Season, SeasonSettlementControlEvent

logger = structlog.get_logger(__name__)


class NoActiveSeason(LookupError):
    pass


class SeasonNotEligible(ValueError):
    pass


class OutsideEconomicWindow(ValueError):
    pass


def economic_window_end(season: Season) -> datetime:
    config = resolve_game_config_version(season.baseline_config_id)
    distribution = config.dlx_distribution
    if distribution is None:
        return season.ends_at
    return season.starts_at + timedelta(days=distribution.epoch_count * distribution.epoch_days)


def is_reward_earning_time(season: Season, occurred_at: datetime) -> bool:
    return season.starts_at <= occurred_at < economic_window_end(season)


def require_reward_earning_time(season: Season, occurred_at: datetime) -> None:
    if not is_reward_earning_time(season, occurred_at):
        raise OutsideEconomicWindow("Source event is outside the Season economic window")


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
        config = resolve_game_config_version(season.baseline_config_id)
        distribution = config.dlx_distribution
        if distribution is not None:
            if season.ends_at - season.starts_at != timedelta(
                days=distribution.product_period_days
            ):
                raise ValidationError("Season dates do not match the configured product period")
            if season.settlement_start_epoch is None:
                season.settlement_start_epoch = distribution.default_settlement_start_epoch
            if not 1 <= season.settlement_start_epoch <= distribution.epoch_count:
                raise ValidationError("Settlement start epoch is outside the configured range")
        if (
            Season.objects.select_for_update()
            .filter(status=Season.Status.ACTIVE)
            .exclude(pk=season.pk)
            .exists()
        ):
            raise ValidationError("An active season already exists")
        season.status = Season.Status.ACTIVE
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('dexo.settlement_control', 'on', true)")
            cursor.execute("SELECT set_config('dexo.season_transition', 'on', true)")
        season.save(update_fields=["status", "settlement_start_epoch", "updated_at"])
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('dexo.settlement_control', 'off', true)")
            cursor.execute("SELECT set_config('dexo.season_transition', 'off', true)")
    logger.info("season_activated", season_id=season.pk, season_code=season.code)
    return season


def set_settlement_start_epoch(
    season: Season,
    epoch: int,
    *,
    actor: Any | None = None,
    service_actor: str = "legacy-service",
    reason: str = "legacy service change",
    ticket_reference: str = "",
    correlation_id: uuid.UUID | None = None,
) -> Season:
    """Change the audited operational gate only before any settlement has begun."""
    with transaction.atomic():
        if actor is not None and (
            not isinstance(actor, User) or not actor.has_perm("seasons.change_settlement_control")
        ):
            raise PermissionDenied("Settlement-control permission required")
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
        if previous is None:
            previous = distribution.default_settlement_start_epoch
        season.settlement_start_epoch = epoch
        if not reason.strip() or (actor is None and not service_actor.strip()):
            raise ValidationError("Settlement control actor and reason are required")
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('dexo.settlement_control', 'on', true)")
        season.save(update_fields=("settlement_start_epoch", "updated_at"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('dexo.settlement_control', 'off', true)")
        SeasonSettlementControlEvent.objects.create(
            season=season,
            previous_start_epoch=previous,
            new_start_epoch=epoch,
            actor=actor,
            service_actor=service_actor if actor is None else "",
            reason=reason,
            ticket_reference=ticket_reference,
            correlation_id=correlation_id or uuid.uuid4(),
        )
    logger.info("settlement_start_epoch_changed", season_id=season.pk, epoch=epoch)
    return season


def archive_closed_season(
    season: Season, *, actor: User, reason: str, ticket_reference: str = ""
) -> Season:
    """Controlled presentation/archive transition after a completed economic close."""
    if not actor.has_perm("seasons.close_season"):
        raise PermissionDenied("Season-close permission required")
    if not reason.strip():
        raise ValidationError("Archive reason is required")
    with transaction.atomic():
        season = Season.objects.select_for_update().get(pk=season.pk)
        if season.status != Season.Status.CLOSED or not getattr(season, "close_run", None):
            raise ValidationError("Only a durably completed closed Season may be archived")
        if season.close_run.stage != "COMPLETED":
            raise ValidationError("Season close is incomplete")
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('dexo.season_transition', 'on', true)")
        season.status = Season.Status.ARCHIVED
        season.save(update_fields=("status", "updated_at"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('dexo.season_transition', 'off', true)")
    logger.info(
        "season_archived",
        season_id=season.pk,
        actor_id=actor.pk,
        ticket_reference=ticket_reference,
    )
    return season
