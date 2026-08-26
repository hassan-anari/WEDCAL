import io
import json
import urllib.error
from email.message import Message
from unittest.mock import patch

import pytest

from apps.telegram.bot_api import get_chat_member


class Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return None

    def read(self, limit: int) -> bytes:
        assert limit == 65536
        return self.payload[:limit]


def http_error(code: int, payload: dict[str, object] | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://redacted", code, "error", Message(), io.BytesIO(json.dumps(payload or {}).encode())
    )


def test_bot_api_success_uses_bounded_request(settings) -> None:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_BOT_TOKEN = "secret-token"
    payload = {"ok": True, "result": {"status": "member", "user": {"id": 42}}}
    with patch(
        "urllib.request.urlopen", return_value=Response(json.dumps(payload).encode())
    ) as call:
        result = get_chat_member(-100, 42)
    assert result.ok and result.result == payload["result"]
    request = call.call_args.args[0]
    assert json.loads(request.data) == {"chat_id": -100, "user_id": 42}
    assert call.call_args.kwargs["timeout"] == 5


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TimeoutError(), "timeout"),
        (urllib.error.URLError("offline"), "connection_error"),
        (OSError("offline"), "connection_error"),
        (http_error(400), "http_error"),
        (http_error(500), "http_error"),
    ],
)
def test_bot_api_classifies_transport_and_http_failures(settings, caplog, error, category) -> None:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_BOT_TOKEN = "never-log-this-token"
    with patch("urllib.request.urlopen", side_effect=error):
        result = get_chat_member(-100, 42)
    assert not result.ok and result.category == category
    assert settings.TELEGRAM_BOT_TOKEN not in caplog.text
    assert settings.TELEGRAM_BOT_TOKEN not in str(result)


def test_bot_api_preserves_429_retry_after_without_token(settings, caplog) -> None:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_BOT_TOKEN = "rate-limit-secret"
    error = http_error(429, {"ok": False, "parameters": {"retry_after": 19}})
    with patch("urllib.request.urlopen", side_effect=error):
        result = get_chat_member(-100, 42)
    assert result.category == "rate_limited" and result.retry_after == 19
    assert settings.TELEGRAM_BOT_TOKEN not in caplog.text


@pytest.mark.parametrize(
    ("payload", "category"),
    [
        (b"not-json", "non_json_response"),
        (json.dumps({"ok": False, "error_code": 400}).encode(), "telegram_not_ok"),
        (json.dumps(["unexpected"]).encode(), "telegram_not_ok"),
    ],
)
def test_bot_api_handles_malformed_and_telegram_not_ok(settings, payload, category) -> None:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_BOT_TOKEN = "secret"
    with patch("urllib.request.urlopen", return_value=Response(payload)):
        result = get_chat_member(-100, 42)
    assert not result.ok and result.category == category
