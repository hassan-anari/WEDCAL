import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import structlog
from django.conf import settings

from apps.telegram.budget import defer_bot_api, reserve_bot_api_budget, retry_after_from_payload

logger = structlog.get_logger(__name__)
SEND_METHODS = {"sendMessage", "copyMessage", "copyMessages"}
AMBIGUOUS_METHODS = SEND_METHODS | {"createChatInviteLink"}


@dataclass(frozen=True, slots=True)
class TelegramResult:
    ok: bool
    result: dict[str, Any] | None = None
    category: str | None = None
    retry_after: int | None = None
    ambiguous: bool = False


def _error_category(code: Any, payload: Any, *, fallback: str) -> str:
    if code == 429:
        return "rate_limited"
    if code == 403:
        return "forbidden"
    if (
        code == 400
        and isinstance(payload, dict)
        and str(payload.get("description", "")).casefold() == "bad request: chat not found"
    ):
        return "chat_not_found"
    if code == 400 and isinstance(payload, dict):
        description = str(payload.get("description", "")).casefold()
        if any(
            term in description
            for term in (
                "message to copy not found",
                "message can't be copied",
                "message can not be copied",
                "protected content",
            )
        ):
            return "source_unavailable"
    return fallback


def request(method: str, parameters: dict[str, Any]) -> TelegramResult:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return TelegramResult(False, category="configuration_error")
    denial = reserve_bot_api_budget(
        method,
        fail_closed=method in SEND_METHODS,
        chat_id=parameters.get("chat_id") if method in SEND_METHODS else None,
        message_count=len(parameters.get("message_ids", [])) if method == "copyMessages" else 1,
    )
    if denial is not None:
        return TelegramResult(False, category=denial.category, retry_after=denial.retry_after)
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
        return TelegramResult(False, category="timeout", ambiguous=method in AMBIGUOUS_METHODS)
    except urllib.error.HTTPError as exc:
        retry_after = None
        error = {}
        try:
            error = json.loads(exc.read(65536))
            retry_after = retry_after_from_payload(error)
        except (ValueError, AttributeError):
            pass
        if exc.code == 429:
            retry_after = retry_after or settings.TELEGRAM_API_DEFAULT_RETRY_SECONDS
            defer_bot_api(retry_after)
        category = _error_category(exc.code, error, fallback="http_error")
        logger.warning(
            "telegram_api_failure",
            method=method,
            category=category,
            status=exc.code,
            retry_after=retry_after,
        )
        return TelegramResult(
            False,
            category=category,
            retry_after=retry_after,
            ambiguous=method in AMBIGUOUS_METHODS and exc.code >= 500,
        )
    except json.JSONDecodeError:
        return TelegramResult(
            False,
            category="non_json_response",
            ambiguous=method in AMBIGUOUS_METHODS,
        )
    except (urllib.error.URLError, OSError):
        return TelegramResult(
            False,
            category="connection_error",
            ambiguous=method in AMBIGUOUS_METHODS,
        )
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        retry = retry_after_from_payload(payload)
        is_429 = isinstance(payload, dict) and payload.get("error_code") == 429
        if is_429:
            retry = retry or settings.TELEGRAM_API_DEFAULT_RETRY_SECONDS
            defer_bot_api(retry)
        return TelegramResult(
            False,
            category=_error_category(
                payload.get("error_code") if isinstance(payload, dict) else None,
                payload,
                fallback="telegram_not_ok",
            ),
            retry_after=retry,
            ambiguous=method in AMBIGUOUS_METHODS
            and isinstance(payload, dict)
            and type(payload.get("error_code")) is int
            and payload["error_code"] >= 500,
        )
    result = payload.get("result")
    if method == "copyMessages":
        if not isinstance(result, list) or any(
            not isinstance(x, dict) or type(x.get("message_id")) is not int for x in result
        ):
            return TelegramResult(False, category="invalid_copy_response", ambiguous=True)
        return TelegramResult(True, {"message_ids": [x["message_id"] for x in result]})
    if method == "copyMessage" and (
        not isinstance(result, dict) or type(result.get("message_id")) is not int
    ):
        return TelegramResult(False, category="invalid_copy_response", ambiguous=True)
    return TelegramResult(True, result if isinstance(result, dict) else {})


def get_chat_member(chat_id: int, user_id: int) -> TelegramResult:
    return request("getChatMember", {"chat_id": chat_id, "user_id": user_id})


def create_chat_invite_link(chat_id: int, name: str) -> TelegramResult:
    return request("createChatInviteLink", {"chat_id": chat_id, "name": name, "member_limit": 1})


def revoke_chat_invite_link(chat_id: int, invite_link: str) -> TelegramResult:
    return request("revokeChatInviteLink", {"chat_id": chat_id, "invite_link": invite_link})


def send_message(chat_id: int, text: str) -> TelegramResult:
    return request("sendMessage", {"chat_id": chat_id, "text": text})
