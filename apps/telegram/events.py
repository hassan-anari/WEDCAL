from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apps.players.services import TelegramProfile


@dataclass(frozen=True, slots=True)
class TelegramUserSeen:
    profile: TelegramProfile
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TelegramMessageObserved:
    chat_id: int
    chat_type: str
    message_id: int
    sender: TelegramProfile | None
    sender_is_bot: bool
    text: str | None
    observed_at: datetime
    chat: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BotStarted:
    user: TelegramProfile
    parameter: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class BotMembershipChanged:
    chat: dict[str, Any]
    bot_user_id: int
    status: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class UserMembershipChanged:
    chat: dict[str, Any]
    telegram_user_id: int
    status: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class GroupMetadataUpdated:
    chat: dict[str, Any]
    observed_at: datetime


NormalizedTelegramEvent = (
    TelegramMessageObserved | BotStarted | BotMembershipChanged | UserMembershipChanged
)
