import json
from typing import Any

import structlog
from django.conf import settings
from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.players.models import Player
from apps.players.services import resolve_player
from apps.telegram.auth import TelegramAuthError, verify_init_data
from apps.telegram.models import TelegramInboundUpdate
from apps.telegram.services import (
    InvalidWebhook,
    dispatch_inbound_update,
    mark_dispatch_failed,
    persist_inbound_update,
    verify_webhook_secret,
)

logger = structlog.get_logger(__name__)
auth_router = Router(tags=["authentication"])
webhook_router = Router(tags=["telegram-webhook"])


class TelegramAuthInput(Schema):
    init_data: str


class PlayerOutput(Schema):
    telegram_user_id: int
    first_name: str
    last_name: str
    username: str
    language_code: str
    created: bool


class ErrorOutput(Schema):
    detail: str


@auth_router.post("/telegram", response={200: PlayerOutput, 401: ErrorOutput})
def authenticate_telegram(
    request: HttpRequest, payload: TelegramAuthInput
) -> Status[dict[str, Any]]:
    try:
        verified = verify_init_data(
            payload.init_data,
            bot_token=settings.TELEGRAM_BOT_TOKEN,
            max_age_seconds=settings.TELEGRAM_INIT_DATA_MAX_AGE_SECONDS,
        )
    except TelegramAuthError:
        return Status(401, {"detail": "Invalid Telegram authentication"})
    resolution = resolve_player(
        verified.profile,
        Player.FirstSeenSource.MINI_APP,
        mini_app=True,
    )
    player = resolution.player
    return Status(
        200,
        {
            "telegram_user_id": player.telegram_user_id,
            "first_name": player.first_name,
            "last_name": player.last_name,
            "username": player.username,
            "language_code": player.language_code,
            "created": resolution.created,
        },
    )


@webhook_router.post(
    "/webhook",
    response={200: dict[str, bool], 400: ErrorOutput, 401: ErrorOutput, 503: ErrorOutput},
)
def telegram_webhook(request: HttpRequest) -> Status[dict[str, Any]]:
    try:
        verify_webhook_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"))
    except InvalidWebhook:
        logger.warning("telegram_webhook_rejected", reason="credentials")
        return Status(401, {"detail": "Unauthorized"})
    try:
        body = json.loads(request.body)
    except (ValueError, TypeError, json.JSONDecodeError):
        return Status(400, {"detail": "Malformed JSON"})
    if not isinstance(body, dict):
        return Status(400, {"detail": "Malformed update"})
    try:
        inbound, created = persist_inbound_update(body)
    except InvalidWebhook:
        return Status(400, {"detail": "Invalid update"})
    if inbound.state in {
        TelegramInboundUpdate.State.PROCESSED,
        TelegramInboundUpdate.State.IGNORED,
    }:
        logger.info("telegram_webhook_duplicate", telegram_update_id=inbound.update_id)
        return Status(200, {"ok": True})
    try:
        dispatch_inbound_update(inbound)
    except Exception as exc:
        mark_dispatch_failed(inbound, exc)
        return Status(503, {"detail": "Dispatch temporarily unavailable"})
    logger.info(
        "telegram_webhook_accepted",
        inbound_update_id=inbound.pk,
        telegram_update_id=inbound.update_id,
        duplicate=not created,
    )
    return Status(200, {"ok": True})
