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


def get_chat_member(chat_id: int, user_id: int) -> TelegramResult:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return TelegramResult(False, category="configuration_error")
    url = f"https://api.telegram.org/bot{token}/getChatMember"
    body = json.dumps({"chat_id": chat_id, "user_id": user_id}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 -- fixed Telegram HTTPS host
            payload = json.loads(response.read(65536))
    except TimeoutError:
        logger.warning("telegram_api_failure", method="getChatMember", category="timeout")
        return TelegramResult(False, category="timeout")
    except urllib.error.HTTPError as exc:
        retry_after = None
        try:
            error = json.loads(exc.read(65536))
            value = error.get("parameters", {}).get("retry_after")
            retry_after = value if type(value) is int else None
        except (ValueError, AttributeError):
            pass
        logger.warning(
            "telegram_api_failure",
            method="getChatMember",
            category="http_error",
            status=exc.code,
            retry_after=retry_after,
        )
        return TelegramResult(
            False,
            category="rate_limited" if exc.code == 429 else "http_error",
            retry_after=retry_after,
        )
    except json.JSONDecodeError:
        logger.warning("telegram_api_failure", method="getChatMember", category="non_json_response")
        return TelegramResult(False, category="non_json_response")
    except (urllib.error.URLError, OSError):
        logger.warning("telegram_api_failure", method="getChatMember", category="connection_error")
        return TelegramResult(False, category="connection_error")
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or not isinstance(payload.get("result"), dict)
    ):
        retry = (
            payload.get("parameters", {}).get("retry_after") if isinstance(payload, dict) else None
        )
        return TelegramResult(
            False, category="telegram_not_ok", retry_after=retry if type(retry) is int else None
        )
    return TelegramResult(True, payload["result"])
