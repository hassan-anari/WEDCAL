"""Capture original operator messages and copy them; never reconstruct media uploads."""

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import BotAdminOperator, BroadcastSource
from apps.telegram import bot_api

COPYABLE = {
    "text",
    "photo",
    "video",
    "voice",
    "audio",
    "document",
    "animation",
    "video_note",
    "sticker",
    "contact",
    "location",
    "venue",
    "dice",
    "poll",
    "story",
}
ALBUM_TYPES = {"photo", "video", "document", "audio"}


def _snapshot(message: dict[str, Any]) -> dict[str, Any]:
    kind = next((key for key in COPYABLE if key in message), None)
    if (
        type(message.get("message_id")) is not int
        or message["message_id"] <= 0
        or not kind
        or message.get("has_protected_content")
    ):
        raise ValidationError("Unsupported or protected message; send a Telegram-copyable original")
    poll = message.get("poll", {})
    if kind == "poll" and poll.get("type") == "quiz" and "correct_option_id" not in poll:
        raise ValidationError("Telegram cannot copy a quiz without its known correct answer")
    # Native references preserve original bytes/entities. No file identifiers are needed.
    return {
        "id": message["message_id"],
        "kind": kind,
        **{
            key: message[key]
            for key in ("text", "caption", "entities", "caption_entities")
            if key in message
        },
    }


@transaction.atomic
def capture(operator: BotAdminOperator, message: dict[str, Any]) -> BroadcastSource:
    part = _snapshot(message)
    now = timezone.now()
    album = message.get("media_group_id", "")
    if (
        not isinstance(album, str)
        or len(album) > 128
        or (album and part["kind"] not in ALBUM_TYPES)
    ):
        raise ValidationError("Invalid media album")
    source = (
        BroadcastSource.objects.select_for_update()
        .filter(operator=operator, media_group_id=album)
        .first()
        if album
        else None
    )
    if source is not None:
        if any(x["id"] == part["id"] for x in source.messages):
            return source
        if source.state != "COLLECTING" or now > source.deadline or len(source.messages) >= 10:
            source.state, source.error_category = "INVALID", "late_or_oversize_album"
            source.save(update_fields=("state", "error_category"))
            return source
        existing = {x["kind"] for x in source.messages}
        if (
            part["kind"] in {"document", "audio"}
            and existing != {part["kind"]}
            or existing & {"document", "audio"}
            and part["kind"] not in existing
        ):
            source.state, source.error_category = "INVALID", "incompatible_album_types"
        else:
            source.messages = sorted([*source.messages, part], key=lambda x: x["id"])
        source.quiet_until = now + timedelta(seconds=settings.BROADCAST_ALBUM_QUIET_SECONDS)
        source.save()
        return source
    return BroadcastSource.objects.create(
        operator=operator,
        chat_id=operator.telegram_user_id,
        media_group_id=album,
        messages=[part],
        state="COLLECTING" if album else "READY",
        sealed_at=None if album else now,
        quiet_until=now + timedelta(seconds=settings.BROADCAST_ALBUM_QUIET_SECONDS),
        deadline=now + timedelta(seconds=settings.BROADCAST_ALBUM_CAPTURE_SECONDS),
    )


@transaction.atomic
def seal_album(source_id: int, *, expected_count: int) -> BroadcastSource:
    source = BroadcastSource.objects.select_for_update().get(pk=source_id)
    now = timezone.now()
    if (
        source.state != "COLLECTING"
        or not source.media_group_id
        or not 2 <= expected_count <= 10
        or len(source.messages) != expected_count
        or now < source.quiet_until
        or now > source.deadline
    ):
        raise ValidationError(
            "Album is incomplete, still arriving or expired. "
            "Wait for all items, then /done N (2–10). Recompose an expired album."
        )
    # Webhook persistence can run ahead of domain processing. Do not seal known pending parts.
    from apps.telegram.models import TelegramInboundUpdate

    known_ids = [x["id"] for x in source.messages]
    if (
        TelegramInboundUpdate.objects.filter(
            payload__message__chat__id=source.chat_id,
            payload__message__media_group_id=source.media_group_id,
        )
        .exclude(payload__message__message_id__in=known_ids)
        .exists()
    ):
        raise ValidationError("More album parts are pending; wait before /done N")
    source.state, source.sealed_at = "READY", now
    source.save(update_fields=("state", "sealed_at"))
    return source


def copy(
    source: BroadcastSource,
    destination: int,
    *,
    preview: bool = False,
    preview_delivery_id: int | None = None,
) -> bot_api.TelegramResult:
    source.refresh_from_db()
    if source.state != "READY":
        return bot_api.TelegramResult(False, category="source_unavailable")
    operator = source.operator
    if (
        not operator.enabled
        or not operator.user.is_active
        or operator.telegram_user_id not in settings.TELEGRAM_ADMIN_IDS
        or not operator.user.has_perm("notifications.manage_broadcasts")
    ):
        return bot_api.TelegramResult(False, category="operator_authorization_revoked")
    # The first successful native preview is bot-authored and cannot be edited by
    # the operator. Every later preview and destination copies that frozen content.
    # Original operator messages can be edited/deleted without changing a campaign.
    frozen = source.frozen_preview
    if frozen is not None:
        ids = frozen.telegram_message_ids if source.media_group_id else [frozen.telegram_message_id]
        if frozen.state != "SENT" or not ids or any(type(value) is not int for value in ids):
            return bot_api.TelegramResult(False, category="source_unavailable")
    elif preview:
        first = (
            source.telegramdelivery_set.filter(
                notification__idempotency_key__startswith="bot-panel:"
            )
            .order_by("pk")
            .first()
        )
        if first is None or first.pk != preview_delivery_id:
            if first and first.state in {"PENDING", "PROCESSING", "RETRY_WAIT"}:
                return bot_api.TelegramResult(False, category="local_rate_limited", retry_after=1)
            return bot_api.TelegramResult(False, category="source_preview_failed")
        ids = [item["id"] for item in source.messages]
    else:
        return bot_api.TelegramResult(False, category="source_unavailable")
    if source.media_group_id:
        result = bot_api.request(
            "copyMessages",
            {"chat_id": destination, "from_chat_id": source.chat_id, "message_ids": ids},
        )
        if result.ok:
            copied = (result.result or {}).get("message_ids", [])
            if len(copied) != len(ids):
                return bot_api.TelegramResult(
                    False, result.result, category="partial_copy", ambiguous=True
                )
        return result
    return bot_api.request(
        "copyMessage",
        {"chat_id": destination, "from_chat_id": source.chat_id, "message_id": ids[0]},
    )


def validate_campaign_source(campaign: Any) -> None:
    if not campaign.copy_source_id:
        return
    # Content is immutable once READY; a late-part invalidation is checked again
    # at dispatch. Avoid taking source locks after the campaign lock (capture
    # holds a source before updating its draft campaign).
    source = BroadcastSource.objects.get(pk=campaign.copy_source_id)
    if source.operator.user_id != campaign.created_by_id:
        raise ValidationError("Source does not belong to this campaign operator")
    if source.state != "READY":
        raise ValidationError("Finish a complete copyable source before confirming")
    # A button can arrive before the asynchronous native preview. Require a successful
    # copy of every item, not just an enqueued request or textual description.
    if (
        not source.frozen_preview_id
        or not source.telegramdelivery_set.filter(
            pk=source.frozen_preview_id,
            state="SENT",
            notification__idempotency_key__startswith="bot-panel:",
        ).exists()
    ):
        raise ValidationError("Wait for the complete native preview, then confirm again")
