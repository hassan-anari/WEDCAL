from collections.abc import Callable

from celery import current_app
from django.core.cache import cache
from django.db import connection
from kombu import Connection

DependencyCheck = Callable[[], None]


def check_postgresql() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def check_redis() -> None:
    cache.set("health:ready", "ok", timeout=5)
    if cache.get("health:ready") != "ok":
        raise ConnectionError("cache round trip failed")


def check_rabbitmq() -> None:
    with Connection(current_app.conf.broker_url, connect_timeout=2) as broker:
        broker.ensure_connection(max_retries=0)


def readiness_status(
    checks: dict[str, DependencyCheck] | None = None,
) -> tuple[bool, dict[str, str]]:
    checks = checks or {
        "postgresql": check_postgresql,
        "redis": check_redis,
        "rabbitmq": check_rabbitmq,
    }
    statuses: dict[str, str] = {}
    for name, check in checks.items():
        try:
            check()
        except Exception:  # Dependency libraries expose unrelated exception hierarchies.
            statuses[name] = "unavailable"
        else:
            statuses[name] = "ok"
    return all(status == "ok" for status in statuses.values()), statuses
