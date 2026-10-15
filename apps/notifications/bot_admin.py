"""Private Telegram control plane. Ingress holds the persisted update lock throughout."""

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.audiences import estimated_targets
from apps.notifications.models import (
    BotAdminInteraction,
    BotAdminOperator,
    BotAdminSession,
    BroadcastCampaign,
    OperatorAuditEvent,
)
from apps.notifications.services import audience_metrics, cancel_campaign, queue_campaign
from apps.players.models import Player
from apps.players.services import TelegramProfile
from apps.telegram.models import TelegramInboundUpdate
from apps.telegram.services import resolve_telegram_player
from apps.telegram.welcome import queue_private_message


def _identity(payload: dict[str, Any]) -> tuple[int, dict[str, Any], str, str] | None:
    callback = payload.get("callback_query")
    message = callback.get("message") if isinstance(callback, dict) else payload.get("message")
    sender = callback.get("from") if isinstance(callback, dict) else (message or {}).get("from")
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return None
    chat = message.get("chat", {})
    user_id = sender.get("id")
    if (
        type(user_id) is not int
        or user_id <= 0
        or sender.get("is_bot")
        or chat.get("type") != "private"
        or type(chat.get("id")) is not int
        or chat.get("id") != user_id
        or any(
            message.get(k)
            for k in (
                "forward_origin",
                "forward_from",
                "forward_from_chat",
                "sender_chat",
                "via_bot",
            )
        )
    ):
        return None
    return (
        user_id,
        sender,
        str(message.get("text", "")),
        str(callback.get("data", "")) if isinstance(callback, dict) else "",
    )


def _audit(session: BotAdminSession, action: str) -> None:
    OperatorAuditEvent.objects.create(
        actor=session.operator.user,
        action=f"BOT_ADMIN_{action}",
        target_type="BotAdminSession",
        target_id=str(session.pk),
        reason="Private authenticated bot action",
        after_state={
            "step": session.step,
            "revision": session.revision,
            "campaign": str(session.campaign.public_id) if session.campaign else None,
        },
    )


def _buttons(session: BotAdminSession, actions: list[tuple[str, str]]) -> dict[str, object]:
    buttons = []
    for label, action in actions:
        token = BotAdminInteraction.objects.create(
            session=session, revision=session.revision, action=action, expires_at=session.expires_at
        )
        buttons.append([{"text": label, "callback_data": f"b3:{token.token.hex}"}])
    return {"inline_keyboard": buttons}


def _reply(session: BotAdminSession, player: Player, inbound: TelegramInboundUpdate) -> None:
    campaign = session.campaign
    actions = [("Broadcast", "compose")]
    text = "Dexo Land Admin\nBroadcast management only."
    if session.step == "COMPOSE":
        text = (
            "Send the exact broadcast text (plain text, at most 4096 characters), "
            "or /cancel to abandon."
        )
        actions = [("Menu", "menu")]
    elif session.step == "AUDIENCE":
        text = (
            "Choose recipients. Only eligible private users and explicitly opted-in "
            "groups are included."
        )
        actions = [
            ("Users", "users"),
            ("Groups", "groups"),
            ("Both", "both"),
            ("Edit message", "edit"),
            ("Abandon draft", "abandon"),
        ]
    elif session.step == "PREVIEW" and campaign:
        counts = estimated_targets(campaign.audience_type)
        queue_private_message(
            player, key=f"bot-panel:{inbound.update_id}:preview", text=campaign.body_snapshot
        )
        text = (
            "Preview above is the exact outgoing plain-text message.\n"
            f"Audience: {campaign.audience_type}\n"
            f"Estimated users: {counts['users']}\nEstimated groups: {counts['groups']}\n"
            "Confirm explicitly to queue. Counts may change before materialization."
        )
        actions = [
            ("Confirm and queue", "confirm"),
            ("Edit message", "edit"),
            ("Change audience", "audience"),
            ("Abandon draft", "abandon"),
        ]
    elif session.step == "TRACK" and campaign:
        campaign.refresh_from_db()
        import json

        text = f"Campaign {campaign.public_id}\n" + json.dumps(audience_metrics(campaign), indent=2)
        actions = [
            ("Refresh progress", "track"),
            ("Cancel unsent deliveries", "cancel"),
            ("Menu", "menu"),
        ]
    elif campaign and campaign.state != "DRAFT":
        actions.append(("Sending progress", "track"))
    elif campaign:
        actions.append(("Resume draft", "audience"))
    queue_private_message(
        player,
        key=f"bot-panel:{inbound.update_id}",
        text=text,
        options={"reply_markup": _buttons(session, actions)},
    )


@transaction.atomic
def handle_private_control(inbound: TelegramInboundUpdate) -> bool:
    payload = inbound.payload
    callback = payload.get("callback_query")
    raw_message = payload.get("message", {})
    command = str(raw_message.get("text", "")).split(" ", 1)[0].split("@", 1)[0]
    if command == "/start" and not isinstance(callback, dict):
        return False  # Preserve welcome/referral routing even while an operator composes a draft.
    is_control = command in {"/admin", "/cancel"} or isinstance(callback, dict)
    identity = _identity(payload)
    if identity is None or inbound.webhook_authenticated_at is None:
        return is_control
    user_id, sender, text, data = identity
    operator = (
        BotAdminOperator.objects.select_related("user")
        .filter(telegram_user_id=user_id, enabled=True)
        .first()
    )
    if (
        not settings.B3_BROADCAST_AUDIENCES_ENABLED
        or user_id not in settings.TELEGRAM_ADMIN_IDS
        or operator is None
        or not operator.user.is_active
        or not operator.user.has_perm("notifications.manage_broadcasts")
    ):
        return is_control
    now = timezone.now()
    session = BotAdminSession.objects.select_for_update().filter(operator=operator).first()
    if not is_control and (
        session is None or session.step != "COMPOSE" or session.expires_at <= now
    ):
        return False
    if (
        session
        and session.expires_at > now
        and command != "/admin"
        and inbound.update_id <= session.last_update_id
    ):
        return True
    if command == "/admin":
        if session is None:
            session = BotAdminSession.objects.create(operator=operator, expires_at=now)
        session.step = "MENU"
        session.expires_at = now + timedelta(seconds=settings.TELEGRAM_ADMIN_SESSION_SECONDS)
    elif session is None or session.expires_at <= now:
        # Expired interactions cannot modify or queue a retained draft.
        return True
    elif command == "/cancel":
        if session.campaign and session.campaign.state != "DRAFT":
            cancel_campaign(
                campaign=session.campaign,
                actor=operator.user,
                reason="Explicit private /cancel command",
            )
            session.step = "TRACK"
        else:
            session.step = "MENU"
            session.campaign = None
    elif data:
        import uuid

        try:
            token = uuid.UUID(data.removeprefix("b3:")) if data.startswith("b3:") else None
        except ValueError:
            return True
        if token is None:
            return True
        interaction = (
            BotAdminInteraction.objects.select_for_update()
            .filter(
                token=token,
                session=session,
                revision=session.revision,
                consumed_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )
        if not interaction:
            return True
        interaction.consumed_at = now
        interaction.save(update_fields=("consumed_at",))
        action = interaction.action
        campaign = session.campaign
        if action in {"compose", "edit"}:
            if not campaign or campaign.state != "DRAFT":
                campaign = BroadcastCampaign.objects.create(
                    created_by=operator.user,
                    audience_type="USERS",
                    body_snapshot="",
                    purpose="Private Bot Admin broadcast",
                )
                session.campaign = campaign
            session.step = "COMPOSE"
        elif action in {"users", "groups", "both"} and campaign and campaign.state == "DRAFT":
            campaign.audience_type = action.upper()
            campaign.save(update_fields=("audience_type",))
            session.step = "PREVIEW"
        elif action == "audience" and campaign and campaign.state == "DRAFT":
            session.step = "AUDIENCE"
        elif (
            action == "confirm"
            and session.step == "PREVIEW"
            and campaign
            and campaign.state == "DRAFT"
        ):
            if not campaign.body_snapshot.strip():
                return True
            queue_campaign(
                campaign=campaign,
                actor=operator.user,
                reason="Explicit private Bot Admin confirmation",
            )
            session.step = "TRACK"
        elif action == "track" and campaign:
            session.step = "TRACK"
        elif action == "cancel" and campaign:
            cancel_campaign(
                campaign=campaign,
                actor=operator.user,
                reason="Explicit private Bot Admin cancellation",
            )
            session.step = "TRACK"
        elif action in {"menu", "abandon"}:
            session.step = "MENU"
            if action == "abandon":
                session.campaign = None
        else:
            return True
    elif session.step == "COMPOSE" and session.campaign and session.campaign.state == "DRAFT":
        if not text.strip() or len(text) > 4096:
            return True
        session.campaign.body_snapshot = text
        session.campaign.save(update_fields=("body_snapshot",))
        session.step = "AUDIENCE"
    else:
        return is_control
    session.revision += 1
    session.last_update_id = inbound.update_id
    session.save()
    _audit(session, "INTERACTION")
    player = resolve_telegram_player(
        TelegramProfile(telegram_user_id=user_id, first_name=str(sender.get("first_name", ""))),
        Player.FirstSeenSource.BOT_START,
        private_contact=True,
        telegram_update_id=inbound.update_id,
    ).player
    if isinstance(callback, dict) and isinstance(callback.get("id"), str):
        queue_private_message(
            player,
            key=f"bot-ack:{inbound.update_id}",
            text="",
            options={"_method": "answerCallbackQuery", "callback_query_id": callback["id"]},
        )
    _reply(session, player, inbound)
    return True
