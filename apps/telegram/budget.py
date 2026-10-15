"""One bot-wide admission budget and rate-limit pause, shared by every Bot API caller."""

import math
import time
from dataclasses import dataclass
from typing import Any

import structlog
from django.conf import settings
from django.core.cache import cache
from django_redis import get_redis_connection  # type: ignore[import-untyped]

logger = structlog.get_logger(__name__)
PAUSE_KEY = "telegram-api:pause-until"
# Native numeric Redis value, atomically extended so a short concurrent 429 cannot shorten a pause.
EXTEND_PAUSE = """
local previous = tonumber(redis.call('GET', KEYS[1]) or '0')
local deadline = tonumber(ARGV[1])
if deadline > previous then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
end
return math.max(previous, deadline)
"""


@dataclass(frozen=True, slots=True)
class BudgetDenial:
    category: str
    retry_after: int


def retry_after_from_payload(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    parameters = payload.get("parameters")
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    return value if type(value) is int and value > 0 else None


def defer_bot_api(retry_after: int) -> None:
    """Share a Telegram 429 pause without logging response text, URLs, IDs or credentials."""
    try:
        get_redis_connection("default").eval(
            EXTEND_PAUSE,
            1,
            cache.make_key(PAUSE_KEY),
            math.ceil(time.time()) + retry_after,
            retry_after + 2,
        )
    except Exception:
        logger.warning("telegram_api_pause_unavailable")


def reserve_bot_api_budget(
    method: str, *, fail_closed: bool = False, chat_id: int | None = None
) -> BudgetDenial | None:
    now = time.time()
    try:
        deadline = get_redis_connection("default").get(cache.make_key(PAUSE_KEY))
        if deadline is not None:
            remaining = math.ceil(float(deadline) - now)
            if remaining > 0:
                return BudgetDenial("rate_limited", remaining)
    except Exception:
        if fail_closed:
            return BudgetDenial("budget_unavailable", settings.TELEGRAM_API_DEFAULT_RETRY_SECONDS)
        # Preserve existing critical-call availability on Redis loss; avatars always fail closed.
    bucket = int(now)
    if method == "sendMessage":
        try:
            key = f"telegram-api:send:{bucket}"
            count = 1 if cache.add(key, 1, timeout=2) else cache.incr(key)
            if count > settings.TELEGRAM_API_SEND_RPS:
                return BudgetDenial("local_rate_limited", 1)
            if chat_id is not None:
                interval = settings.TELEGRAM_API_GROUP_INTERVAL_SECONDS if chat_id < 0 else 1
                if not cache.add(f"telegram-api:chat:{chat_id}", 1, timeout=interval):
                    return BudgetDenial("local_rate_limited", interval)
        except Exception:
            return BudgetDenial("budget_unavailable", settings.TELEGRAM_API_DEFAULT_RETRY_SECONDS)
    for key, limit in (
        (f"telegram-api:global:{bucket}", settings.TELEGRAM_API_GLOBAL_RPS),
        (f"telegram-api:method:{method}:{bucket}", settings.TELEGRAM_API_METHOD_RPS),
    ):
        try:
            count = 1 if cache.add(key, 1, timeout=2) else cache.incr(key)
        except Exception:
            if fail_closed:
                return BudgetDenial(
                    "budget_unavailable", settings.TELEGRAM_API_DEFAULT_RETRY_SECONDS
                )
            continue
        if count > limit:
            logger.warning("telegram_api_budget_exhausted", method=method)
            return BudgetDenial("local_rate_limited", 1)
    return None
