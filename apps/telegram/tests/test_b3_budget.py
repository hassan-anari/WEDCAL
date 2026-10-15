# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from types import SimpleNamespace

import pytest
from django.core.cache.backends.locmem import LocMemCache

from apps.telegram import budget


@pytest.fixture
def sending_budget(monkeypatch, settings):
    cache = LocMemCache("b3-budget", {})
    cache.clear()
    monkeypatch.setattr(budget, "cache", cache)
    monkeypatch.setattr(
        budget, "get_redis_connection", lambda *_: SimpleNamespace(get=lambda *_: None)
    )
    monkeypatch.setattr(budget, "time", SimpleNamespace(time=lambda: 1000))
    settings.TELEGRAM_API_SEND_RPS = 2
    settings.TELEGRAM_API_GLOBAL_RPS = 5
    settings.TELEGRAM_API_METHOD_RPS = 5
    return cache


def test_broadcast_send_budget_cannot_starve_membership_and_shared_methods(sending_budget):
    assert budget.reserve_bot_api_budget("sendMessage", chat_id=10) is None
    assert budget.reserve_bot_api_budget("sendMessage", chat_id=-10) is None
    for _ in range(20):
        denied = budget.reserve_bot_api_budget("sendMessage", chat_id=11)
        assert denied is not None and denied.category == "local_rate_limited"
    assert budget.reserve_bot_api_budget("getChatMember") is None
    assert budget.reserve_bot_api_budget("getUserProfilePhotos", fail_closed=True) is None
    assert budget.reserve_bot_api_budget("getFile", fail_closed=True) is None


@pytest.mark.parametrize("chat_id,expected", [(10, 1), (-10, 3)])
def test_chat_budget_is_shared_and_conservative(sending_budget, settings, chat_id, expected):
    settings.TELEGRAM_API_SEND_RPS = 10
    assert budget.reserve_bot_api_budget("sendMessage", chat_id=chat_id) is None
    denied = budget.reserve_bot_api_budget("sendMessage", chat_id=chat_id)
    assert (
        denied is not None
        and denied.category == "local_rate_limited"
        and denied.retry_after == expected
    )
