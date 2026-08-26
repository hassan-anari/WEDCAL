import hashlib
import secrets
import unicodedata
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.periods import resolve_daily_period
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_effective_game_config
from apps.network.models import (
    CreatorDailyAllowance,
    CreatorGroupRegistration,
    CreatorRegistrationIntent,
    CreatorWindowSettlement,
    NetworkActivity,
)
from apps.players.models import Player
from apps.rewards.services import settle_player_reward
from apps.seasons.services import resolve_current_season
from apps.telegram.bot_api import get_chat_member
from apps.telegram.models import TelegramGroup


def create_registration_intent(creator: Player) -> tuple[CreatorRegistrationIntent, str]:
    raw = secrets.token_urlsafe(24)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    intent = CreatorRegistrationIntent.objects.create(
        creator=creator,
        token_hash=digest,
        expires_at=timezone.now()
        + timedelta(seconds=settings.NETWORK_REGISTRATION_INTENT_TTL_SECONDS),
    )
    return intent, f"https://t.me/{settings.TELEGRAM_BOT_USERNAME.lstrip('@')}?startgroup={raw}"


def consume_registration_token(
    raw: str, sender: Player, group: TelegramGroup
) -> CreatorGroupRegistration | None:
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with transaction.atomic():
        intent = (
            CreatorRegistrationIntent.objects.select_for_update()
            .filter(
                token_hash=digest,
                creator=sender,
                consumed_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if not intent:
            return None
        registration, _ = CreatorGroupRegistration.objects.get_or_create(
            group=group, defaults={"creator": sender}
        )
        if registration.creator_id != sender.pk:
            return None
        intent.consumed_at = timezone.now()
        intent.save(update_fields=("consumed_at",))
        group.is_monitored = True
        group.save(update_fields=("is_monitored", "updated_at"))
    return registration


def verify_registration(registration_id: int) -> str:
    registration = CreatorGroupRegistration.objects.select_related("group", "creator").get(
        pk=registration_id
    )
    bot_id = settings.TELEGRAM_BOT_ID
    if bot_id is None:
        raise RuntimeError("telegram_bot_id_unavailable")
    bot = get_chat_member(registration.group.telegram_chat_id, bot_id)
    creator = get_chat_member(
        registration.group.telegram_chat_id, registration.creator.telegram_user_id
    )
    if not bot.ok or not creator.ok:
        category = bot.category if not bot.ok else creator.category
        if category in {"timeout", "rate_limited", "connection_error"}:
            raise RuntimeError(category)
        state = CreatorGroupRegistration.State.ADMIN_REQUIRED
    else:
        bot_admin = (
            bot.result.get("status") in {"administrator", "creator"} if bot.result else False
        )
        creator_admin = (
            creator.result.get("status") in {"administrator", "creator"}
            if creator.result
            else False
        )
        state = (
            CreatorGroupRegistration.State.ACTIVE
            if bot_admin and creator_admin
            else CreatorGroupRegistration.State.ADMIN_REQUIRED
        )
    with transaction.atomic():
        locked = CreatorGroupRegistration.objects.select_for_update().get(pk=registration_id)
        Player.objects.select_for_update().get(pk=locked.creator_id)
        config = resolve_effective_game_config(timezone.now())
        if (
            state == CreatorGroupRegistration.State.ACTIVE
            and CreatorGroupRegistration.objects.filter(creator=locked.creator, state=state)
            .exclude(pk=locked.pk)
            .count()
            >= config.network.max_active_groups
        ):
            state = CreatorGroupRegistration.State.ADMIN_REQUIRED
        locked.state = state
        locked.last_verification_error = ""
        if state == CreatorGroupRegistration.State.ACTIVE:
            locked.bot_verified_at = timezone.now()
            locked.creator_verified_at = timezone.now()
        locked.save()
    return state


def fingerprints(text: str) -> tuple[str, str]:
    exact = unicodedata.normalize("NFC", text)
    normalized = " ".join(exact.casefold().split())
    return hashlib.sha256(exact.encode()).hexdigest(), hashlib.sha256(
        normalized.encode()
    ).hexdigest()


def record_activity(
    *,
    registration: CreatorGroupRegistration,
    sender: Player | None,
    message_id: int,
    update_id: int,
    text: str | None,
    observed_at: datetime,
    sender_is_bot: bool = False,
    control: bool = False,
) -> NetworkActivity | None:
    config = resolve_effective_game_config(observed_at)
    season = resolve_current_season(at=observed_at, required=False)
    body = text or ""
    exact, normalized = fingerprints(body) if body else ("", "")
    classification = NetworkActivity.Classification.REJECTED
    rewardable = 0
    active = registration.state == CreatorGroupRegistration.State.ACTIVE
    if control:
        classification = NetworkActivity.Classification.CONTROL
    elif (
        sender
        and not sender_is_bot
        and body
        and sender.moderation_state
        not in {Player.ModerationState.SUSPENDED, Player.ModerationState.BANNED}
        and len(body) >= config.network.min_message_chars
    ):
        minute_start = observed_at - timedelta(seconds=60)
        rate = NetworkActivity.objects.filter(
            sender=sender, observed_at__gt=minute_start, observed_at__lte=observed_at
        ).count()
        duplicate = (
            NetworkActivity.objects.filter(
                sender=sender,
                normalized_fingerprint=normalized,
                observed_at__gt=observed_at
                - timedelta(seconds=config.network.normalized_duplicate_window_seconds),
            ).count()
            + 1
        )
        if (
            rate < config.network.max_messages_per_minute
            and duplicate < config.network.reject_count
        ):
            classification = (
                NetworkActivity.Classification.SUSPICIOUS
                if duplicate >= config.network.suspicious_count
                else NetworkActivity.Classification.VALID
            )
            if active and sender.pk != registration.creator_id:
                rewardable = min(len(body), config.network.max_rewardable_chars)
    try:
        return NetworkActivity.objects.create(
            registration=registration,
            group=registration.group,
            sender=sender,
            telegram_message_id=message_id,
            telegram_update_id=update_id,
            observed_at=observed_at,
            character_count=len(body),
            rewardable_chars=rewardable,
            classification=classification,
            text=body or None,
            exact_fingerprint=exact,
            normalized_fingerprint=normalized,
            config_version_id=config.id,
            season=season,
            program_active=active,
            content_expires_at=observed_at + timedelta(days=config.retention.message_content_days),
            fingerprint_expires_at=observed_at + timedelta(days=config.retention.fingerprint_days),
        )
    except IntegrityError:
        return None


def settle_window(
    registration_id: int, window_start: datetime, config_id: int, season_id: int
) -> CreatorWindowSettlement:
    config_model = GameConfigVersion.objects.get(pk=config_id)
    config = resolve_effective_game_config(window_start)
    registration = CreatorGroupRegistration.objects.select_related("creator").get(
        pk=registration_id
    )
    end = window_start + timedelta(seconds=config.network.window_seconds)
    rows = NetworkActivity.objects.filter(
        registration=registration,
        season_id=season_id,
        config_version_id=config_id,
        observed_at__gte=window_start,
        observed_at__lt=end,
        program_active=True,
        rewardable_chars__gt=0,
    )
    chars = rows.aggregate(total=Sum("rewardable_chars"))["total"] or 0
    speakers = rows.values("sender_id").distinct().count()
    raw = (
        min(chars // config.network.chars_per_point, config.network.max_points_per_window)
        if speakers >= config.network.min_unique_speakers
        else 0
    )
    period = resolve_daily_period(
        window_start,
        config.daily.reset_timezone,
        config.daily.reset_hour,
        config.daily.reset_minute,
    )
    with transaction.atomic():
        settlement, created = CreatorWindowSettlement.objects.get_or_create(
            registration=registration,
            season_id=season_id,
            config_version=config_model,
            window_start=window_start,
            defaults={"eligible_chars": chars, "unique_speakers": speakers},
        )
        if not created:
            return settlement
        Player.objects.select_for_update().get(pk=registration.creator_id)
        allowance, _ = CreatorDailyAllowance.objects.select_for_update().get_or_create(
            creator=registration.creator, period_key=period.key
        )
        points = min(raw, max(config.network.max_points_per_creator_day - allowance.points, 0))
        if points:
            reward, _ = settle_player_reward(
                player=registration.creator,
                source_type="CREATOR",
                source_id=f"window:{settlement.pk}",
                context="NETWORK_GROUP",
                amount=points,
                idempotency_key=f"creator:window:{settlement.pk}",
                occurred_at=end,
                resolved_config=resolve_effective_game_config(end),
            )
            settlement.points = points
            settlement.reward = reward
            settlement.save(update_fields=("points", "reward"))
            allowance.points += points
            allowance.save(update_fields=("points",))
    return settlement


def purge_activity_evidence(now: datetime | None = None) -> tuple[int, int]:
    now = now or timezone.now()
    content = (
        NetworkActivity.objects.filter(content_expires_at__lte=now)
        .exclude(text__isnull=True)
        .update(text=None)
    )
    fingerprints_count = (
        NetworkActivity.objects.filter(fingerprint_expires_at__lte=now)
        .exclude(exact_fingerprint="", normalized_fingerprint="")
        .update(exact_fingerprint="", normalized_fingerprint="")
    )
    return content, fingerprints_count
