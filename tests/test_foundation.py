from urllib.parse import urlparse

from django.conf import settings
from django.core import checks
from django.core.cache import cache
from django.test import Client

from dexo_land.api import api
from dexo_land.celery import app


def test_django_system_checks_pass() -> None:
    assert checks.run_checks() == []


def test_admin_route_exists(client: Client) -> None:
    response = client.get("/admin/")
    assert response.status_code == 302
    assert "/admin/login/" in response.headers["Location"]


def test_ninja_api_initializes(client: Client) -> None:
    assert api.title == "Dexo Land API"
    assert client.get("/api/v1/openapi.json").status_code == 200


def test_test_settings_are_deterministic() -> None:
    assert settings.SETTINGS_MODULE == "dexo_land.settings.test"
    assert settings.DEBUG is False
    assert settings.TIME_ZONE == "UTC"
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"
    assert settings.DATABASES["default"]["CONN_MAX_AGE"] == 0


def test_celery_uses_rabbitmq_without_results() -> None:
    scheme = urlparse(app.conf.broker_url).scheme
    assert scheme in {"amqp", "pyamqp"}
    assert scheme != "redis"
    assert app.conf.task_ignore_result is True
    assert app.conf.result_backend is None


def test_telegram_ingress_retention_is_scheduled() -> None:
    schedule = settings.CELERY_BEAT_SCHEDULE["purge-expired-telegram-payloads"]
    assert schedule["task"] == "apps.telegram.tasks.purge_expired_telegram_payloads"
    assert schedule["schedule"] == settings.TELEGRAM_INGRESS_PURGE_INTERVAL_SECONDS


def test_redis_cache_round_trip() -> None:
    cache.set("foundation:test", "ok", timeout=5)
    assert cache.get("foundation:test") == "ok"
