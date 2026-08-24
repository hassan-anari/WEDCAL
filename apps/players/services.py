from dataclasses import dataclass
from datetime import datetime

import structlog
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.players.models import Player

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramProfile:
    telegram_user_id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    photo_url: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerResolution:
    player: Player
    created: bool


def resolve_player(
    profile: TelegramProfile,
    source: Player.FirstSeenSource,
    *,
    observed_at: datetime | None = None,
    private_contact: bool = False,
    mini_app: bool = False,
    start_parameter: str | None = None,
    telegram_update_id: int | None = None,
) -> PlayerResolution:
    """Resolve by DB-enforced Telegram identity and enrich without wiping omitted data."""
    observed_at = observed_at or timezone.now()
    defaults = {
        "first_seen_at": observed_at,
        "first_seen_source": source,
        "last_seen_at": observed_at,
        "profile_observed_at": observed_at,
        "profile_last_telegram_update_id": telegram_update_id,
    }
    try:
        with transaction.atomic():
            player, created = Player.objects.get_or_create(
                telegram_user_id=profile.telegram_user_id, defaults=defaults
            )
    except IntegrityError:
        # A concurrent transaction won the unique-key race. The failed savepoint has
        # rolled back, so resolving the committed canonical row is safe.
        player = Player.objects.get(telegram_user_id=profile.telegram_user_id)
        created = False

    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player.pk)
        changed: list[str] = []
        profile_is_newer = (
            telegram_update_id is not None
            and (
                player.profile_last_telegram_update_id is None
                or telegram_update_id > player.profile_last_telegram_update_id
            )
        ) or (
            telegram_update_id is None
            and (player.profile_observed_at is None or observed_at > player.profile_observed_at)
        )
        for field in ("first_name", "last_name", "username", "language_code", "photo_url"):
            value = getattr(profile, field)
            current_value = getattr(player, field)
            if (
                value is not None
                and value != current_value
                and (profile_is_newer or not current_value)
            ):
                setattr(player, field, value)
                changed.append(field)
        if profile_is_newer:
            player.profile_observed_at = observed_at
            changed.append("profile_observed_at")
            if telegram_update_id is not None:
                player.profile_last_telegram_update_id = telegram_update_id
                changed.append("profile_last_telegram_update_id")
        if observed_at > player.last_seen_at:
            player.last_seen_at = observed_at
            changed.append("last_seen_at")
        if private_contact and player.first_private_contact_at is None:
            player.first_private_contact_at = observed_at
            changed.append("first_private_contact_at")
        if mini_app and player.first_mini_app_at is None:
            player.first_mini_app_at = observed_at
            changed.append("first_mini_app_at")
        if start_parameter and not player.first_start_parameter:
            player.first_start_parameter = start_parameter[:255]
            changed.append("first_start_parameter")
        if changed:
            player.save(update_fields=[*changed, "updated_at"])

    if created:
        logger.info("player_created", player_id=player.pk)
    return PlayerResolution(player=player, created=created)
