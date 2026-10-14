import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl

from apps.core.api_errors import ErrorCode
from apps.players.services import TelegramProfile


class TelegramAuthError(ValueError):
    def __init__(self, message: str, *, code: ErrorCode = "AUTH_INVALID") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class VerifiedInitData:
    profile: TelegramProfile
    auth_date: datetime


def verify_init_data(
    raw_init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int,
    now: datetime | None = None,
) -> VerifiedInitData:
    if not bot_token:
        raise TelegramAuthError("Telegram authentication is unavailable", code="AUTH_UNAVAILABLE")
    if not raw_init_data:
        raise TelegramAuthError("Telegram initData is required", code="AUTH_MISSING")
    try:
        pairs = parse_qsl(
            raw_init_data, strict_parsing=True, keep_blank_values=True, errors="strict"
        )
    except (ValueError, UnicodeError) as exc:
        raise TelegramAuthError("Malformed Telegram initData") from exc
    data: dict[str, str] = {}
    for key, value in pairs:
        if key in data:
            raise TelegramAuthError("Duplicate Telegram initData field")
        data[key] = value
    supplied_hash = data.pop("hash", None)
    if not supplied_hash:
        raise TelegramAuthError("Missing Telegram initData hash")
    # compare_digest(str, str) raises TypeError for non-ASCII input.
    if re.fullmatch(r"[0-9a-f]{64}", supplied_hash) is None:
        raise TelegramAuthError("Invalid Telegram initData hash")
    data_check_string = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    try:
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    except UnicodeError as exc:
        raise TelegramAuthError("Malformed Telegram initData") from exc
    if not hmac.compare_digest(expected_hash, supplied_hash):
        raise TelegramAuthError("Invalid Telegram initData hash")
    try:
        auth_date = datetime.fromtimestamp(int(data["auth_date"]), tz=UTC)
    except (KeyError, ValueError, OverflowError, OSError) as exc:
        raise TelegramAuthError("Invalid Telegram auth_date") from exc
    current_time = now or datetime.now(tz=UTC)
    age_seconds = (current_time - auth_date).total_seconds()
    if age_seconds < 0:
        raise TelegramAuthError("Invalid Telegram auth_date")
    if age_seconds > max_age_seconds:
        raise TelegramAuthError("Expired Telegram initData", code="AUTH_EXPIRED")
    try:
        user: Any = json.loads(data["user"])
        telegram_user_id = user["id"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramAuthError("Malformed Telegram user") from exc
    if (
        not isinstance(user, dict)
        or type(telegram_user_id) is not int
        or not 0 < telegram_user_id < 2**63
        or user.get("is_bot")
    ):
        raise TelegramAuthError("Invalid Telegram user")
    return VerifiedInitData(
        profile=TelegramProfile(
            telegram_user_id=telegram_user_id,
            first_name=_optional_string(user, "first_name"),
            last_name=_optional_string(user, "last_name"),
            username=_optional_string(user, "username"),
            language_code=_optional_string(user, "language_code"),
            photo_url=_optional_string(user, "photo_url"),
        ),
        auth_date=auth_date,
    )


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if isinstance(item, str):
        limit = 2048 if key == "photo_url" else 35 if key == "language_code" else 255
        if len(item) > limit or "\x00" in item:
            raise TelegramAuthError("Invalid Telegram profile field")
    return item if isinstance(item, str) else None
