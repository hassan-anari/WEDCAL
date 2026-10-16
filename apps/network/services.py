import hashlib
import secrets
from datetime import datetime, timedelta

import structlog
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.activity import fingerprints as fingerprints
from apps.core.activity import window_start_for as window_start_for
from apps.core.periods import resolve_daily_period
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import (
    ResolvedGameConfig,
    resolve_effective_game_config,
    resolve_game_config_version,
)
from apps.network.models import (
    CreatorDailyAllowance,
    CreatorGroupRegistration,
    CreatorRegistrationIntent,
    CreatorRegistrationStateEvent,
    CreatorWindowSettlement,
    NetworkActivity,
)
from apps.players.models import Player
from apps.rewards.services import settle_player_reward
from apps.seasons.models import Season
from apps.seasons.services import (
    economic_window_end,
    is_reward_earning_time,
    resolve_current_season,
    resolve_historical_season,
)
from apps.telegram.bot_api import get_chat_member
from apps.telegram.models import TelegramGroup

logger = structlog.get_logger(__name__)


def create_registration_intent(
    creator: Player, *, authorize_group_messages: bool = False
) -> tuple[CreatorRegistrationIntent, str]:
    config = resolve_effective_game_config(timezone.now())
    if not config.network.enabled:
        raise ValueError("Network program is disabled")
    raw = secrets.token_urlsafe(24)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    intent = CreatorRegistrationIntent.objects.create(
        creator=creator,
        authorize_group_messages=authorize_group_messages,
        token_hash=digest,
        expires_at=timezone.now()
        + timedelta(seconds=settings.NETWORK_REGISTRATION_INTENT_TTL_SECONDS),
    )
    return intent, f"https://t.me/{settings.TELEGRAM_BOT_USERNAME.lstrip('@')}?startgroup={raw}"


def consume_registration_token(
    raw: str, sender: Player, group: TelegramGroup, *, observed_at: datetime | None = None
) -> CreatorGroupRegistration | None:
    observed_at = observed_at or timezone.now()
    config = resolve_effective_game_config(observed_at)
    if not config.network.enabled:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with transaction.atomic():
        group = TelegramGroup.objects.select_for_update().get(pk=group.pk)
        from apps.clans.models import ClanSeason

        if ClanSeason.objects.filter(
            clan__telegram_group=group,
            state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
        ).exists():
            return None
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
        registration, created = CreatorGroupRegistration.objects.get_or_create(
            group=group, defaults={"creator": sender, "authorization_intent": intent}
        )
        if registration.creator_id != sender.pk:
            return None
        CreatorRegistrationStateEvent.objects.get_or_create(
            registration=registration,
            occurred_at=observed_at,
            source="TOKEN_CONSUMED",
            defaults={"state": registration.state, "reward_eligible": False},
        )
        intent.consumed_at = timezone.now()
        intent.save(update_fields=("consumed_at",))
        group.is_monitored = True
        group.save(update_fields=("is_monitored", "updated_at"))
        from apps.network.tasks import dispatch_verification

        transaction.on_commit(lambda: dispatch_verification(registration.pk))
    return registration


def resolve_intent_creator(raw: str, telegram_user_id: int) -> Player | None:
    digest = hashlib.sha256(raw.encode()).hexdigest()
    intent = (
        CreatorRegistrationIntent.objects.select_related("creator")
        .filter(
            token_hash=digest,
            creator__telegram_user_id=telegram_user_id,
            consumed_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    return intent.creator if intent else None


def verify_registration(registration_id: int) -> str:
    registration = CreatorGroupRegistration.objects.select_related("group", "creator").get(
        pk=registration_id
    )
    if registration.state in {
        CreatorGroupRegistration.State.SUSPENDED,
        CreatorGroupRegistration.State.INACTIVE,
    }:
        return registration.state
    if not resolve_effective_game_config(timezone.now()).network.enabled:
        return CreatorGroupRegistration.State.ADMIN_REQUIRED
    bot_id = settings.TELEGRAM_BOT_ID
    if bot_id is None:
        raise RuntimeError("telegram_bot_id_unavailable")
    bot = get_chat_member(registration.group.telegram_chat_id, bot_id)
    creator = get_chat_member(
        registration.group.telegram_chat_id, registration.creator.telegram_user_id
    )
    if not bot.ok or not creator.ok:
        category = bot.category if not bot.ok else creator.category
        retry_after = bot.retry_after if not bot.ok else creator.retry_after
        jitter = registration_id % max(settings.TELEGRAM_API_RETRY_JITTER_SECONDS, 1)
        delay = max(retry_after or settings.TELEGRAM_API_DEFAULT_RETRY_SECONDS, 1) + jitter
        CreatorGroupRegistration.objects.filter(pk=registration_id).update(
            last_verification_error=category or "verification_error",
            next_verification_at=timezone.now() + timedelta(seconds=delay),
        )
        if category in {
            "timeout",
            "rate_limited",
            "connection_error",
            "non_json_response",
            "telegram_not_ok",
            "local_rate_limited",
        }:
            raise RuntimeError(category)
        state = CreatorGroupRegistration.State.ADMIN_REQUIRED
    else:
        bot_admin = (
            bot.result.get("status") in {"administrator", "creator"}
            and (bot.result.get("user") or {}).get("id") == bot_id
            if bot.result
            else False
        )
        creator_admin = (
            creator.result.get("status") in {"administrator", "creator"}
            and (creator.result.get("user") or {}).get("id")
            == registration.creator.telegram_user_id
            if creator.result
            else False
        )
        state = (
            CreatorGroupRegistration.State.ACTIVE
            if bot_admin and creator_admin
            else CreatorGroupRegistration.State.ADMIN_REQUIRED
        )
    with transaction.atomic():
        group_id = CreatorGroupRegistration.objects.values_list("group_id", flat=True).get(
            pk=registration_id
        )
        locked_group = TelegramGroup.objects.select_for_update().get(pk=group_id)
        if (
            locked_group.bot_membership_last_update_id
            != registration.group.bot_membership_last_update_id
        ):
            if not locked_group.bot_is_admin:
                state = CreatorGroupRegistration.State.ADMIN_REQUIRED
        elif bot.ok and bot.result and (bot.result.get("user") or {}).get("id") == bot_id:
            locked_group.bot_membership_status = str(bot.result.get("status", ""))
            locked_group.bot_is_admin = bot.result.get("status") in {"creator", "administrator"}
            locked_group.save(update_fields=["bot_membership_status", "bot_is_admin", "updated_at"])
        locked = CreatorGroupRegistration.objects.select_for_update().get(pk=registration_id)
        if locked.state in {
            CreatorGroupRegistration.State.SUSPENDED,
            CreatorGroupRegistration.State.INACTIVE,
        }:
            return locked.state
        Player.objects.select_for_update().get(pk=locked.creator_id)
        config = resolve_effective_game_config(timezone.now())
        from apps.clans.models import ClanSeason

        if (
            state == CreatorGroupRegistration.State.ACTIVE
            and ClanSeason.objects.filter(
                clan__telegram_group_id=locked.group_id,
                state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
            ).exists()
        ):
            state = CreatorGroupRegistration.State.ADMIN_REQUIRED
        if (
            state == CreatorGroupRegistration.State.ACTIVE
            and CreatorGroupRegistration.objects.filter(creator=locked.creator, state=state)
            .exclude(pk=locked.pk)
            .count()
            >= config.network.max_active_groups
        ):
            state = CreatorGroupRegistration.State.ADMIN_REQUIRED
        locked.state = state
        if state == CreatorGroupRegistration.State.ACTIVE:
            if locked.reward_eligible_since is None:
                locked.reward_eligible_since = timezone.now()
        else:
            locked.reward_eligible_since = None
        locked.next_verification_at = timezone.now() + timedelta(minutes=5)
        locked.last_verification_error = ""
        if state == CreatorGroupRegistration.State.ACTIVE:
            locked.bot_verified_at = timezone.now()
            locked.creator_verified_at = timezone.now()
        locked.save()
        CreatorRegistrationStateEvent.objects.create(
            registration=locked,
            state=locked.state,
            reward_eligible=locked.state == CreatorGroupRegistration.State.ACTIVE,
            occurred_at=timezone.now(),
            source="VERIFICATION",
        )
    return state


def registration_reward_eligible_at(registration: CreatorGroupRegistration, at: datetime) -> bool:
    event = (
        registration.state_events.filter(occurred_at__lte=at)
        .order_by("-occurred_at", "-pk")
        .first()
    )
    if event is not None:
        return event.reward_eligible
    # Backward-compatible provenance for rows created before state-event history.
    return bool(registration.reward_eligible_since and registration.reward_eligible_since <= at)


def deactivate_registration_at(registration_id: int, *, at: datetime, source: str) -> None:
    with transaction.atomic():
        group_id = CreatorGroupRegistration.objects.values_list("group_id", flat=True).get(
            pk=registration_id
        )
        TelegramGroup.objects.select_for_update().get(pk=group_id)
        registration = CreatorGroupRegistration.objects.select_for_update().get(pk=registration_id)
        if registration.state != CreatorGroupRegistration.State.ACTIVE:
            return
        registration.state = CreatorGroupRegistration.State.ADMIN_REQUIRED
        registration.reward_eligible_since = None
        registration.save(update_fields=("state", "reward_eligible_since", "updated_at"))
        CreatorRegistrationStateEvent.objects.create(
            registration=registration,
            state=registration.state,
            reward_eligible=False,
            occurred_at=at,
            source=source,
        )


def _window_lock_key(
    registration_id: int, season_id: int | None, config_id: int, window_start: datetime
) -> int:
    raw = f"{registration_id}:{season_id or 0}:{config_id}:{window_start.isoformat()}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


def _lock_window(
    registration_id: int, season_id: int | None, config_id: int, window_start: datetime
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            [_window_lock_key(registration_id, season_id, config_id, window_start)],
        )


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
    season = resolve_historical_season(at=observed_at, required=False)
    start = window_start_for(observed_at, config.network.window_seconds)
    with transaction.atomic():
        if season is not None:
            from apps.seasons.barrier import acquire_economic_write_barrier

            acquire_economic_write_barrier(season.pk)
        _lock_window(registration.pk, season.pk if season else None, config.id, start)
        return _record_activity_locked(
            registration=registration,
            sender=sender,
            message_id=message_id,
            update_id=update_id,
            text=text,
            observed_at=observed_at,
            sender_is_bot=sender_is_bot,
            control=control,
            config=config,
            season=season,
            start=start,
        )


def _record_activity_locked(
    *,
    registration: CreatorGroupRegistration,
    sender: Player | None,
    message_id: int,
    update_id: int,
    text: str | None,
    observed_at: datetime,
    sender_is_bot: bool,
    control: bool,
    config: ResolvedGameConfig,
    season: Season | None,
    start: datetime,
) -> NetworkActivity | None:
    body = text or ""
    exact, normalized = fingerprints(body) if body else ("", "")
    classification = NetworkActivity.Classification.REJECTED
    rewardable = 0
    registration = CreatorGroupRegistration.objects.get(pk=registration.pk)
    active = bool(
        config.network.enabled
        and season is not None
        and is_reward_earning_time(season, observed_at)
        and registration_reward_eligible_at(registration, observed_at)
    )
    finalized = CreatorWindowSettlement.objects.filter(
        registration=registration,
        season=season,
        config_version_id=config.id,
        window_start=start,
        finalized_at__isnull=False,
    ).exists()
    if finalized:
        classification = NetworkActivity.Classification.LATE
    elif control:
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
        normalized_duplicate = (
            NetworkActivity.objects.filter(
                sender=sender,
                normalized_fingerprint=normalized,
                observed_at__gt=observed_at
                - timedelta(seconds=config.network.normalized_duplicate_window_seconds),
                observed_at__lte=observed_at,
            ).count()
            + 1
        )
        exact_duplicate = (
            NetworkActivity.objects.filter(
                sender=sender,
                exact_fingerprint=exact,
                observed_at__gt=observed_at
                - timedelta(seconds=config.network.duplicate_window_seconds),
                observed_at__lte=observed_at,
            ).count()
            + 1
        )
        duplicate = max(normalized_duplicate, exact_duplicate)
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
        with transaction.atomic():
            activity = NetworkActivity.objects.create(
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
                window_processed=finalized,
                content_expires_at=observed_at
                + timedelta(days=config.retention.message_content_days),
                fingerprint_expires_at=observed_at
                + timedelta(days=config.retention.fingerprint_days),
            )
            if (
                config.network.speaker_enabled
                and config.network.speaker_points_per_unit > 0
                and rewardable > 0
                and sender
                and season
                and season.status == Season.Status.ACTIVE
            ):
                reward, _ = settle_player_reward(
                    player=sender,
                    source_type="NETWORK_SPEAKER",
                    source_id=f"activity:{activity.pk}",
                    context="NETWORK_GROUP",
                    amount=config.network.speaker_points_per_unit,
                    idempotency_key=f"network:speaker:{activity.pk}",
                    occurred_at=observed_at,
                    resolved_config=config,
                    source_season=season,
                    preserve_config_version=True,
                )
                activity.speaker_reward = reward
                activity.save(update_fields=("speaker_reward",))
        return activity
    except IntegrityError:
        return None


def settle_window(
    registration_id: int, window_start: datetime, config_id: int, season_id: int
) -> CreatorWindowSettlement:
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier

        acquire_economic_write_barrier(season_id)
        _lock_window(registration_id, season_id, config_id, window_start)
        return _settle_window_locked(registration_id, window_start, config_id, season_id)


def _settle_window_locked(
    registration_id: int, window_start: datetime, config_id: int, season_id: int
) -> CreatorWindowSettlement:
    config_model = GameConfigVersion.objects.get(pk=config_id)
    config = resolve_game_config_version(config_id)
    registration = CreatorGroupRegistration.objects.select_related("creator").get(
        pk=registration_id
    )
    if not config.network.enabled:
        raise ValueError("Network program is disabled")
    source_season = Season.objects.get(pk=season_id)
    season = resolve_current_season(at=window_start, required=False)
    end = window_start + timedelta(seconds=config.network.window_seconds)
    window_rows = NetworkActivity.objects.filter(
        registration=registration,
        season_id=season_id,
        config_version_id=config_id,
        observed_at__gte=window_start,
        observed_at__lt=end,
    )
    eligible_rows = window_rows.filter(
        program_active=True,
        rewardable_chars__gt=0,
    )
    chars = eligible_rows.aggregate(total=Sum("rewardable_chars"))["total"] or 0
    speakers = eligible_rows.values("sender_id").distinct().count()
    if not season or season.pk != season_id:
        settlement, _ = CreatorWindowSettlement.objects.get_or_create(
            registration=registration,
            season=source_season,
            config_version=config_model,
            window_start=window_start,
            defaults={
                "state": CreatorWindowSettlement.State.DEFERRED_SEASON_CLOSE,
                "finalized_at": timezone.now(),
                "eligible_chars": chars,
                "unique_speakers": speakers,
            },
        )
        window_rows.update(window_processed=True)
        return settlement
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
            defaults={
                "eligible_chars": chars,
                "unique_speakers": speakers,
                "finalized_at": timezone.now(),
            },
        )
        if not created:
            window_rows.update(window_processed=True)
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
                occurred_at=min(
                    end - timedelta(microseconds=1),
                    economic_window_end(season) - timedelta(microseconds=1),
                    season.ends_at - timedelta(microseconds=1),
                ),
                resolved_config=config,
                source_season=season,
                preserve_config_version=True,
            )
            settlement.points = points
            settlement.reward = reward
            settlement.state = CreatorWindowSettlement.State.REWARDED
            settlement.save(update_fields=("points", "reward", "state"))
            allowance.points += points
            allowance.save(update_fields=("points",))
        window_rows.update(window_processed=True)
    return settlement


def settle_completed_windows(*, limit: int = 200, now: datetime | None = None) -> int:
    now = now or timezone.now()
    candidates = list(
        NetworkActivity.objects.filter(window_processed=False, observed_at__lt=now)
        .order_by("observed_at", "id")
        .values("registration_id", "season_id", "config_version_id", "observed_at")[:limit]
    )
    processed = 0
    seen: set[tuple[int, int, int, datetime]] = set()
    for row in candidates:
        if row["season_id"] is None:
            NetworkActivity.objects.filter(
                registration_id=row["registration_id"],
                season_id__isnull=True,
                config_version_id=row["config_version_id"],
            ).update(window_processed=True)
            continue
        config = resolve_game_config_version(row["config_version_id"])
        start = window_start_for(row["observed_at"], config.network.window_seconds)
        identity = (
            row["registration_id"],
            row["season_id"],
            row["config_version_id"],
            start,
        )
        grace = max(
            config.network.duplicate_window_seconds,
            config.network.normalized_duplicate_window_seconds,
        )
        if (
            identity in seen
            or start + timedelta(seconds=config.network.window_seconds + grace) > now
        ):
            continue
        seen.add(identity)
        try:
            settle_window(
                registration_id=row["registration_id"],
                window_start=start,
                config_id=row["config_version_id"],
                season_id=row["season_id"],
            )
        except Exception as exc:
            logger.warning(
                "creator_window_settlement_failed",
                registration_id=row["registration_id"],
                season_id=row["season_id"],
                config_version_id=row["config_version_id"],
                window_start=start.isoformat(),
                error_category=type(exc).__name__,
            )
            continue
        processed += 1
    return processed


def purge_activity_evidence(now: datetime | None = None, *, limit: int = 1000) -> tuple[int, int]:
    from apps.moderation.services import protected_network_activity_ids

    now = now or timezone.now()
    held = protected_network_activity_ids(now)
    content_ids = list(
        NetworkActivity.objects.filter(content_expires_at__lte=now)
        .exclude(pk__in=held)
        .exclude(text__isnull=True)
        .order_by("content_expires_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    content = NetworkActivity.objects.filter(pk__in=content_ids).update(text=None)
    fingerprint_ids = list(
        NetworkActivity.objects.filter(fingerprint_expires_at__lte=now)
        .exclude(pk__in=held)
        .exclude(exact_fingerprint="", normalized_fingerprint="")
        .order_by("fingerprint_expires_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    fingerprints_count = NetworkActivity.objects.filter(pk__in=fingerprint_ids).update(
        exact_fingerprint="", normalized_fingerprint=""
    )
    return content, fingerprints_count
