# mypy: disable-error-code="no-untyped-def"
import json
from typing import Any
from unittest.mock import Mock

import pytest
from django.core.cache import cache

from apps.telegram import bot_api
from apps.telegram.budget import reserve_bot_api_budget


@pytest.fixture
def copy_http(settings, monkeypatch):
    settings.TELEGRAM_BOT_TOKEN = "123456:synthetic"
    monkeypatch.setattr(bot_api, "reserve_bot_api_budget", lambda *_a, **_kw: None)

    def response(payload):
        value = Mock()
        value.__enter__ = Mock(return_value=value)
        value.__exit__ = Mock(return_value=False)
        value.read.return_value = json.dumps(payload).encode()
        monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: value)

    return response


def test_copy_messages_native_result_and_partial_shape(copy_http):
    copy_http({"ok": True, "result": [{"message_id": 5}, {"message_id": 6}]})
    result = bot_api.request(
        "copyMessages", {"chat_id": 9, "from_chat_id": 1, "message_ids": [2, 3]}
    )
    assert result.ok and result.result == {"message_ids": [5, 6]}
    malformed: list[Any] = [None, {}, [{"not_message_id": 5}], [{"message_id": True}]]
    for value in malformed:
        copy_http({"ok": True, "result": value})
        result = bot_api.request("copyMessages", {})
        assert not result.ok and result.ambiguous


@pytest.mark.parametrize("method", ["copyMessage", "copyMessages"])
def test_copy_timeout_connection_and_error_response_are_safe(copy_http, method, monkeypatch):
    def timeout(*_args, **_kw):
        raise TimeoutError("sensitive synthetic transport text")

    monkeypatch.setattr("urllib.request.urlopen", timeout)
    assert bot_api.request(method, {}).ambiguous
    copy_http({"ok": False, "error_code": 500, "description": "synthetic"})
    assert bot_api.request(method, {}).ambiguous
    copy_http(
        {"ok": False, "error_code": 400, "description": "Bad Request: message to copy not found"}
    )
    result = bot_api.request(method, {})
    assert result.category == "source_unavailable" and not result.ambiguous
    copy_http(
        {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}
    )
    assert bot_api.request(method, {}).category == "forbidden"


def test_copies_share_send_and_per_chat_budgets_and_album_cost(settings, monkeypatch):
    # Use the same actual Redis cache/budget paths as existing send budget tests.
    cache.clear()
    monkeypatch.setattr(
        "apps.telegram.budget.get_redis_connection", lambda *_: Mock(get=lambda *_: None)
    )
    settings.TELEGRAM_API_SEND_RPS = 10
    settings.TELEGRAM_API_GLOBAL_RPS = 100
    settings.TELEGRAM_API_METHOD_RPS = 100
    assert (
        reserve_bot_api_budget("copyMessages", fail_closed=True, chat_id=-1, message_count=8)
        is None
    )
    denied = reserve_bot_api_budget("sendMessage", fail_closed=True, chat_id=-1)
    assert denied and denied.category == "local_rate_limited"
    assert reserve_bot_api_budget("copyMessage", fail_closed=True, chat_id=2) is None
    denied = reserve_bot_api_budget("copyMessages", fail_closed=True, chat_id=3, message_count=3)
    assert denied and denied.category == "local_rate_limited"
