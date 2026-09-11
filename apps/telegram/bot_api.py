import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramResult:
    ok: bool
    result: dict[str, Any] | None = None
    category: str | None = None
    retry_after: int | None = None
    ambiguous: bool = False


def request(method: str, parameters: dict[str, Any]) -> TelegramResult:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return TelegramResult(False, category="configuration_error")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(parameters).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # noqa: S310
            payload = json.loads(response.read(65536))
    except TimeoutError:
        logger.warning("telegram_api_failure", method=method, category="timeout")
        return TelegramResult(False, category="timeout", ambiguous=method == "createChatInviteLink")
    except urllib.error.HTTPError as exc:
        retry_after = None
        try:
            error = json.loads(exc.read(65536))
            value = error.get("parameters", {}).get("retry_after")
            retry_after = value if type(value) is int else None
        except (ValueError, AttributeError):
            pass
        category = "rate_limited" if exc.code == 429 else "http_error"
        logger.warning(
            "telegram_api_failure",
            method=method,
            category=category,
            status=exc.code,
            retry_after=retry_after,
        )
        return TelegramResult(False, category=category, retry_after=retry_after)
    except json.JSONDecodeError:
        return TelegramResult(
            False, category="non_json_response", ambiguous=method == "createChatInviteLink"
        )
    except (urllib.error.URLError, OSError):
        return TelegramResult(
            False, category="connection_error", ambiguous=method == "createChatInviteLink"
        )
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        retry = (
            payload.get("parameters", {}).get("retry_after") if isinstance(payload, dict) else None
        )
        return TelegramResult(
            False, category="telegram_not_ok", retry_after=retry if type(retry) is int else None
        )
    result = payload.get("result")
    return TelegramResult(True, result if isinstance(result, dict) else {})


def get_chat_member(chat_id: int, user_id: int) -> TelegramResult:
    return request("getChatMember", {"chat_id": chat_id, "user_id": user_id})


def create_chat_invite_link(chat_id: int, name: str) -> TelegramResult:
    return request("createChatInviteLink", {"chat_id": chat_id, "name": name, "member_limit": 1})


def revoke_chat_invite_link(chat_id: int, invite_link: str) -> TelegramResult:
    return request("revokeChatInviteLink", {"chat_id": chat_id, "invite_link": invite_link})


def send_message(chat_id: int, text: str) -> TelegramResult:
    return request("sendMessage", {"chat_id": chat_id, "text": text})
