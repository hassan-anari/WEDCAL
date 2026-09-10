from datetime import UTC, datetime
from typing import Any

from apps.players.services import TelegramProfile
from apps.telegram.events import (
    BotMembershipChanged,
    BotStarted,
    NormalizedTelegramEvent,
    TelegramMessageObserved,
    UserMembershipChanged,
)


def normalize_update(payload: dict[str, Any]) -> NormalizedTelegramEvent | None:
    if isinstance(payload.get("message"), dict):
        return _normalize_message(payload["message"])
    if isinstance(payload.get("my_chat_member"), dict):
        item = payload["my_chat_member"]
        new_member = item.get("new_chat_member", {})
        user = new_member.get("user", {})
        return BotMembershipChanged(
            chat=item.get("chat", {}),
            bot_user_id=int(user["id"]),
            status=str(new_member["status"]),
            observed_at=_timestamp(item.get("date")),
            can_invite_users=bool(new_member.get("can_invite_users")),
        )
    if isinstance(payload.get("chat_member"), dict):
        item = payload["chat_member"]
        new_member = item.get("new_chat_member", {})
        user = new_member.get("user", {})
        return UserMembershipChanged(
            chat=item.get("chat", {}),
            telegram_user_id=int(user["id"]),
            status=str(new_member["status"]),
            observed_at=_timestamp(item.get("date")),
            previous_status=str(item.get("old_chat_member", {}).get("status", "")),
            is_member=(
                new_member.get("is_member")
                if isinstance(new_member.get("is_member"), bool)
                else None
            ),
            actor_telegram_user_id=(
                int(item["from"]["id"])
                if isinstance(item.get("from"), dict) and "id" in item["from"]
                else None
            ),
        )
    return None


def _normalize_message(message: dict[str, Any]) -> NormalizedTelegramEvent | None:
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    observed_at = _timestamp(message.get("date"))
    profile = TelegramProfile(
        telegram_user_id=int(sender["id"]),
        first_name=_string(sender.get("first_name")),
        last_name=_string(sender.get("last_name")),
        username=_string(sender.get("username")),
        language_code=_string(sender.get("language_code")),
    )
    text = _string(message.get("text"))
    if chat.get("type") == "private" and text:
        command, _, parameter = text.partition(" ")
        if command.split("@", 1)[0] == "/start":
            return BotStarted(profile, parameter.strip() or None, observed_at)
    return TelegramMessageObserved(
        chat_id=int(chat["id"]),
        chat_type=str(chat.get("type", "")),
        message_id=int(message["message_id"]),
        sender=profile,
        sender_is_bot=bool(sender.get("is_bot")),
        text=text,
        observed_at=observed_at,
        chat=chat,
    )


def _timestamp(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
