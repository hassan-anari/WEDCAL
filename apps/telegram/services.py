import secrets
from datetime import datetime, timedelta
from typing import Any

import structlog
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.players.models import Player
from apps.players.services import PlayerResolution, TelegramProfile, resolve_player
from apps.telegram.events import (
    BotMembershipChanged,
    BotStarted,
    TelegramMessageObserved,
    UserMembershipChanged,
)
from apps.telegram.models import TelegramGroup, TelegramInboundUpdate, TelegramUserMembership
from apps.telegram.normalizer import normalize_update

logger = structlog.get_logger(__name__)


class InvalidWebhook(ValueError):
    pass


def verify_webhook_secret(supplied: str | None) -> None:
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected or supplied is None or not secrets.compare_digest(supplied, expected):
        raise InvalidWebhook("Invalid Telegram webhook credentials")


def persist_inbound_update(payload: dict[str, Any]) -> tuple[TelegramInboundUpdate, bool]:
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        raise InvalidWebhook("Invalid Telegram update_id")
    expires_at = timezone.now() + timedelta(seconds=settings.TELEGRAM_INGRESS_RETENTION_SECONDS)
    try:
        with transaction.atomic():
            inbound, created = TelegramInboundUpdate.objects.get_or_create(
                update_id=update_id,
                defaults={"payload": payload, "payload_expires_at": expires_at},
            )
    except IntegrityError:
        inbound = TelegramInboundUpdate.objects.get(update_id=update_id)
        created = False
    return inbound, created


def dispatch_inbound_update(inbound: TelegramInboundUpdate) -> None:
    from apps.telegram.tasks import process_telegram_update

    TelegramInboundUpdate.objects.filter(pk=inbound.pk).update(
        dispatch_attempts=F("dispatch_attempts") + 1
    )
    process_telegram_update.delay(inbound.pk)
    TelegramInboundUpdate.objects.filter(pk=inbound.pk).update(
        dispatched_at=timezone.now(), last_error_category=""
    )


def mark_dispatch_failed(inbound: TelegramInboundUpdate, error: Exception) -> None:
    TelegramInboundUpdate.objects.filter(pk=inbound.pk).update(
        last_error_category=type(error).__name__[:100]
    )
    logger.error(
        "telegram_ingress_dispatch_failed",
        inbound_update_id=inbound.pk,
        telegram_update_id=inbound.update_id,
        error_category=type(error).__name__,
    )


@transaction.atomic
def process_inbound_update(inbound_id: int) -> None:
    inbound = TelegramInboundUpdate.objects.select_for_update().get(pk=inbound_id)
    if inbound.state in {
        TelegramInboundUpdate.State.PROCESSED,
        TelegramInboundUpdate.State.IGNORED,
    }:
        return
    inbound.processing_attempts = F("processing_attempts") + 1
    inbound.save(update_fields=("processing_attempts", "updated_at"))
    event = normalize_update(inbound.payload)
    if event is None:
        inbound.state = TelegramInboundUpdate.State.IGNORED
    else:
        _apply_event(event, telegram_update_id=inbound.update_id)
        inbound.state = TelegramInboundUpdate.State.PROCESSED
    inbound.processed_at = timezone.now()
    inbound.last_error_category = ""
    inbound.save(update_fields=("state", "processed_at", "last_error_category", "updated_at"))
    logger.info(
        "telegram_update_processed",
        inbound_update_id=inbound.pk,
        telegram_update_id=inbound.update_id,
        outcome=inbound.state,
    )


def record_processing_failure(inbound_id: int, error: Exception) -> None:
    TelegramInboundUpdate.objects.filter(pk=inbound_id).update(
        state=TelegramInboundUpdate.State.FAILED,
        last_error_category=type(error).__name__[:100],
    )
    logger.error(
        "telegram_update_processing_failed",
        inbound_update_id=inbound_id,
        error_category=type(error).__name__,
    )


def set_group_monitoring(group: TelegramGroup, *, enabled: bool) -> TelegramGroup:
    group.is_monitored = enabled
    group.save(update_fields=("is_monitored", "updated_at"))
    return group


def purge_expired_ingress_payloads() -> int:
    """Remove sensitive raw payloads while retaining dedupe/audit metadata."""
    return (
        TelegramInboundUpdate.objects.filter(
            payload_expires_at__lte=timezone.now(),
            state__in=(
                TelegramInboundUpdate.State.PROCESSED,
                TelegramInboundUpdate.State.IGNORED,
            ),
        )
        .exclude(payload={})
        .update(payload={})
    )


@transaction.atomic
def resolve_telegram_player(
    profile: TelegramProfile,
    source: Player.FirstSeenSource,
    *,
    observed_at: datetime | None = None,
    private_contact: bool = False,
    mini_app: bool = False,
    start_parameter: str | None = None,
    telegram_update_id: int | None = None,
) -> PlayerResolution:
    _lock_telegram_user(profile.telegram_user_id)
    resolution = resolve_player(
        profile,
        source,
        observed_at=observed_at,
        private_contact=private_contact,
        mini_app=mini_app,
        start_parameter=start_parameter,
        telegram_update_id=telegram_update_id,
    )
    TelegramUserMembership.objects.filter(
        telegram_user_id=profile.telegram_user_id, player__isnull=True
    ).update(player=resolution.player)
    return resolution


def _lock_telegram_user(telegram_user_id: int) -> None:
    """Serialize only identity/membership reconciliation for one Telegram user."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [telegram_user_id])


def _apply_event(event: object, *, telegram_update_id: int) -> None:
    if isinstance(event, BotStarted):
        resolution = resolve_telegram_player(
            event.user,
            Player.FirstSeenSource.BOT_START,
            observed_at=event.observed_at,
            private_contact=True,
            start_parameter=event.parameter,
            telegram_update_id=telegram_update_id,
        )
        from apps.referrals.services import attribute_start

        attribute_start(resolution.player, event.parameter, at=event.observed_at)
    elif isinstance(event, TelegramMessageObserved):
        _observe_message(event, telegram_update_id)
    elif isinstance(event, BotMembershipChanged):
        _observe_bot_membership(event, telegram_update_id)
    elif isinstance(event, UserMembershipChanged):
        _observe_user_membership(event, telegram_update_id)


def _upsert_group(
    chat: dict[str, Any], observed_at: datetime, telegram_update_id: int
) -> TelegramGroup:
    chat_id = int(chat["id"])
    chat_type = str(chat["type"])
    if chat_type not in TelegramGroup.ChatType.values:
        raise ValueError("Unsupported Telegram group type")
    group, created = TelegramGroup.objects.get_or_create(
        telegram_chat_id=chat_id,
        defaults={
            "chat_type": chat_type,
            "title": str(chat.get("title", "")),
            "username": str(chat.get("username", "")),
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
            "metadata_last_update_id": telegram_update_id,
        },
    )
    group = TelegramGroup.objects.select_for_update().get(pk=group.pk)
    changed: list[str] = []
    metadata_is_newer = (
        group.metadata_last_update_id is None or telegram_update_id > group.metadata_last_update_id
    )
    for field, value in (
        ("chat_type", chat_type),
        ("title", chat.get("title")),
        ("username", chat.get("username")),
    ):
        current_value = getattr(group, field)
        if (
            value is not None
            and str(value) != current_value
            and (metadata_is_newer or not current_value)
        ):
            setattr(group, field, str(value))
            changed.append(field)
    if observed_at > group.last_seen_at:
        group.last_seen_at = observed_at
        changed.append("last_seen_at")
    if metadata_is_newer:
        group.metadata_last_update_id = telegram_update_id
        changed.append("metadata_last_update_id")
    if changed:
        group.save(update_fields=(*changed, "updated_at"))
    if created:
        logger.info("telegram_group_observed", telegram_group_id=group.pk)
    return group


def _observe_message(event: TelegramMessageObserved, telegram_update_id: int) -> None:
    if event.chat_type not in TelegramGroup.ChatType.values:
        return
    group = _upsert_group(event.chat, event.observed_at, telegram_update_id)
    if event.sender and not event.sender_is_bot:
        resolution = resolve_telegram_player(
            event.sender,
            Player.FirstSeenSource.GROUP_ACTIVITY,
            observed_at=event.observed_at,
            telegram_update_id=telegram_update_id,
        )
        command = (event.text or "").split(maxsplit=1)
        if command and command[0].split("@", 1)[0] == "/start" and len(command) == 2:
            from apps.network.services import consume_registration_token

            consume_registration_token(command[1], resolution.player, group)
            return
        try:
            registration = group.creator_registration
        except ObjectDoesNotExist:
            return
        from apps.network.services import record_activity

        record_activity(
            registration=registration,
            sender=resolution.player,
            message_id=event.message_id,
            update_id=telegram_update_id,
            text=event.text,
            observed_at=event.observed_at,
            sender_is_bot=event.sender_is_bot,
        )


def _observe_bot_membership(event: BotMembershipChanged, telegram_update_id: int) -> None:
    expected_bot_id = settings.TELEGRAM_BOT_ID
    if expected_bot_id is not None and event.bot_user_id != expected_bot_id:
        logger.warning("telegram_bot_identity_mismatch")
        return
    group = _upsert_group(event.chat, event.observed_at, telegram_update_id)
    if (
        group.bot_membership_last_update_id is not None
        and telegram_update_id <= group.bot_membership_last_update_id
    ):
        return
    group.bot_membership_status = event.status
    group.bot_is_admin = event.status == "administrator"
    group.bot_membership_last_update_id = telegram_update_id
    group.save(
        update_fields=(
            "bot_membership_status",
            "bot_is_admin",
            "bot_membership_last_update_id",
            "updated_at",
        )
    )


def _observe_user_membership(event: UserMembershipChanged, telegram_update_id: int) -> None:
    group = _upsert_group(event.chat, event.observed_at, telegram_update_id)
    _lock_telegram_user(event.telegram_user_id)
    player = Player.objects.filter(telegram_user_id=event.telegram_user_id).first()
    membership, created = TelegramUserMembership.objects.select_for_update().get_or_create(
        group=group,
        telegram_user_id=event.telegram_user_id,
        defaults={
            "player": player,
            "status": event.status,
            "is_admin": event.status == "administrator",
            "is_owner": event.status == "creator",
            "observed_at": event.observed_at,
            "last_update_id": telegram_update_id,
        },
    )
    if created or telegram_update_id <= membership.last_update_id:
        return
    membership.player = player
    membership.status = event.status
    membership.is_admin = event.status == "administrator"
    membership.is_owner = event.status == "creator"
    membership.observed_at = event.observed_at
    membership.last_update_id = telegram_update_id
    membership.save(
        update_fields=(
            "player",
            "status",
            "is_admin",
            "is_owner",
            "observed_at",
            "last_update_id",
            "updated_at",
        )
    )
