import secrets
from datetime import datetime, timedelta
from typing import Any

import structlog
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.players.models import Player
from apps.players.services import resolve_player
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
        _apply_event(event)
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
        TelegramInboundUpdate.objects.filter(payload_expires_at__lte=timezone.now())
        .exclude(payload={})
        .update(payload={})
    )


def _apply_event(event: object) -> None:
    if isinstance(event, BotStarted):
        resolve_player(
            event.user,
            Player.FirstSeenSource.BOT_START,
            observed_at=event.observed_at,
            private_contact=True,
            start_parameter=event.parameter,
        )
    elif isinstance(event, TelegramMessageObserved):
        _observe_message(event)
    elif isinstance(event, BotMembershipChanged):
        _observe_bot_membership(event)
    elif isinstance(event, UserMembershipChanged):
        _observe_user_membership(event)


def _upsert_group(chat: dict[str, Any], observed_at: datetime) -> TelegramGroup:
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
        },
    )
    changed: list[str] = []
    for field, value in (
        ("chat_type", chat_type),
        ("title", chat.get("title")),
        ("username", chat.get("username")),
    ):
        if value is not None and str(value) != getattr(group, field):
            setattr(group, field, str(value))
            changed.append(field)
    if observed_at > group.last_seen_at:
        group.last_seen_at = observed_at
        changed.append("last_seen_at")
    if changed:
        group.save(update_fields=(*changed, "updated_at"))
    if created:
        logger.info("telegram_group_observed", telegram_group_id=group.pk)
    return group


def _observe_message(event: TelegramMessageObserved) -> None:
    if event.chat_type not in TelegramGroup.ChatType.values:
        return
    group = _upsert_group(event.chat, event.observed_at)
    if group.is_monitored and event.sender and not event.sender_is_bot:
        resolve_player(
            event.sender,
            Player.FirstSeenSource.GROUP_ACTIVITY,
            observed_at=event.observed_at,
        )


def _observe_bot_membership(event: BotMembershipChanged) -> None:
    expected_bot_id = settings.TELEGRAM_BOT_ID
    if expected_bot_id is not None and event.bot_user_id != expected_bot_id:
        logger.warning("telegram_bot_identity_mismatch")
        return
    group = _upsert_group(event.chat, event.observed_at)
    group.bot_membership_status = event.status
    group.bot_is_admin = event.status == "administrator"
    group.save(update_fields=("bot_membership_status", "bot_is_admin", "updated_at"))


def _observe_user_membership(event: UserMembershipChanged) -> None:
    group = _upsert_group(event.chat, event.observed_at)
    player = Player.objects.filter(telegram_user_id=event.telegram_user_id).first()
    TelegramUserMembership.objects.update_or_create(
        group=group,
        telegram_user_id=event.telegram_user_id,
        defaults={
            "player": player,
            "status": event.status,
            "is_admin": event.status == "administrator",
            "is_owner": event.status == "creator",
            "observed_at": event.observed_at,
        },
    )
