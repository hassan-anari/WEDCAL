import io
import json
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from email.message import Message
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from urllib.error import HTTPError
from uuid import uuid4

import pytest
from django.core.cache.backends.locmem import LocMemCache

from apps.players import avatars
from apps.telegram import bot_api, budget


class PauseRedis:
    def __init__(self) -> None:
        self.deadline = 0

    def get(self, key: str) -> int:
        return self.deadline

    def eval(self, script: str, count: int, key: str, until: int, ttl: int) -> int:
        self.deadline = max(self.deadline, until)
        return self.deadline


@pytest.fixture
def isolated_budget(settings: Any, monkeypatch: Any) -> Any:
    settings.TELEGRAM_BOT_TOKEN = "123456:never-log-test-token"
    cache = LocMemCache(f"telegram-budget-{uuid4()}", {})
    redis = PauseRedis()
    monkeypatch.setattr(budget, "cache", cache)
    monkeypatch.setattr(budget, "get_redis_connection", lambda *args: redis)
    monkeypatch.setattr(budget, "time", SimpleNamespace(time=lambda: 1000.5))
    return redis


def test_avatar_debits_the_same_global_budget_as_other_bot_methods(
    isolated_budget: Any, settings: Any, monkeypatch: Any
) -> None:
    settings.TELEGRAM_API_GLOBAL_RPS = 1
    # A critical Bot API method reserves the same global counter used by avatar transport.
    assert budget.reserve_bot_api_budget("getChatMember") is None
    dns = Mock(side_effect=AssertionError("budget denial must precede DNS/network"))
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    with pytest.raises(avatars.AvatarRetry, match="local_rate_limited"):
        avatars.bot_call("getUserProfilePhotos", {"user_id": 1})
    dns.assert_not_called()


def test_avatar_fails_closed_when_shared_budget_is_unavailable(
    isolated_budget: Any, monkeypatch: Any
) -> None:
    def unavailable(*args: Any) -> Any:
        raise OSError("cache unavailable")

    monkeypatch.setattr(budget, "get_redis_connection", unavailable)
    dns = Mock(side_effect=AssertionError("no outbound call on budget outage"))
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    with pytest.raises(avatars.AvatarRetry, match="budget_unavailable"):
        avatars.bot_call("getFile", {"file_id": "one"})
    dns.assert_not_called()


@pytest.mark.parametrize("origin", ["avatar_http", "avatar_json", "other_http", "other_json"])
def test_429_from_any_caller_pauses_all_bot_api_methods(
    isolated_budget: Any, monkeypatch: Any, origin: str, caplog: Any
) -> None:
    payload = {"ok": False, "error_code": 429, "parameters": {"retry_after": 1234}}
    error = HTTPError(
        "https://redacted", 429, "rate", Message(), io.BytesIO(json.dumps(payload).encode())
    )
    if origin == "avatar_http":
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [(2, 1, 6, "", ("149.154.167.220", 443))],
        )
        opener = SimpleNamespace(open=Mock(side_effect=error))
        monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
        with pytest.raises(avatars.AvatarRetry) as exc:
            avatars.bot_call("getFile", {"file_id": "one"})
        assert exc.value.retry_after == 1234
    elif origin == "avatar_json":
        monkeypatch.setattr(
            avatars, "_telegram_request", lambda *args, **kwargs: json.dumps(payload).encode()
        )
        with pytest.raises(avatars.AvatarRetry) as exc:
            avatars.bot_call("getFile", {"file_id": "one"})
        assert exc.value.retry_after == 1234
    else:
        response = SimpleNamespace()

        class Response:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *args: Any) -> None:
                pass

            def read(self, limit: int) -> bytes:
                return json.dumps(payload).encode()

        response = (
            Mock(side_effect=error) if origin == "other_http" else Mock(return_value=Response())
        )
        monkeypatch.setattr(urllib.request, "urlopen", response)
        result = bot_api.get_chat_member(-1, 1)
        assert result.category == "rate_limited" and result.retry_after == 1234
    outbound = Mock(side_effect=AssertionError("shared Telegram pause must prevent requests"))
    monkeypatch.setattr(urllib.request, "urlopen", outbound)
    result = bot_api.get_chat_member(-1, 1)
    assert (
        not result.ok
        and result.category == "rate_limited"
        and result.retry_after is not None
        and result.retry_after >= 1234
    )
    outbound.assert_not_called()
    denial = budget.reserve_bot_api_budget("getUserProfilePhotos", fail_closed=True)
    assert denial and denial.category == "rate_limited" and denial.retry_after >= 1234
    assert "never-log-test-token" not in caplog.text


def test_retry_payload_is_typed_and_positive() -> None:
    for value in (True, -1, 0, "10", None):
        assert budget.retry_after_from_payload({"parameters": {"retry_after": value}}) is None
    assert budget.retry_after_from_payload({"parameters": {"retry_after": 19}}) == 19
    assert budget.retry_after_from_payload({"parameters": "invalid"}) is None


def test_real_redis_concurrent_429_never_shortens_longest_pause(settings: Any) -> None:
    from django.core.cache import cache
    from django_redis import get_redis_connection  # type: ignore[import-untyped]

    values = deepcopy(settings.CACHES)
    values["default"]["KEY_PREFIX"] = f"b2-budget-test-{uuid4()}"
    settings.CACHES = values
    client = get_redis_connection("default")
    key = cache.make_key(budget.PAUSE_KEY)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(budget.defer_bot_api, (9, 31, 2, 70, 4, 12, 60, 1)))
        remaining = budget.reserve_bot_api_budget("getFile", fail_closed=True)
        assert remaining and remaining.retry_after >= 69
        assert client.ttl(key) >= 69
    finally:
        client.delete(key)
