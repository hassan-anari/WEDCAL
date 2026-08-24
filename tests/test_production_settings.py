import os
import subprocess
import sys

import pytest

REQUIRED_PRODUCTION_ENVIRONMENT = {
    "DJANGO_SECRET_KEY": "a-production-secret-with-at-least-fifty-characters-1234567890",
    "DJANGO_ALLOWED_HOSTS": "example.com",
    "POSTGRES_DB": "dexo_land",
    "POSTGRES_USER": "dexo_land",
    "POSTGRES_PASSWORD": "database-password",
    "POSTGRES_HOST": "postgres.example.com",
    "POSTGRES_PORT": "5432",
    "REDIS_URL": "redis://redis.example.com:6379/0",
    "CELERY_BROKER_URL": "amqp://user:password@rabbitmq.example.com:5672//",
}


def import_production_settings(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import dexo_land.settings.production"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("missing_name", REQUIRED_PRODUCTION_ENVIRONMENT)
def test_production_settings_reject_missing_required_configuration(missing_name: str) -> None:
    environment = os.environ.copy()
    environment.update(REQUIRED_PRODUCTION_ENVIRONMENT)
    environment.pop(missing_name)
    result = import_production_settings(environment)
    assert result.returncode != 0
    assert missing_name in result.stderr


@pytest.mark.parametrize("module", ["dexo_land.asgi", "dexo_land.wsgi", "dexo_land.celery"])
def test_runtime_entrypoints_do_not_default_to_local_settings(module: str) -> None:
    environment = os.environ.copy()
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "DJANGO_SETTINGS_MODULE" in result.stderr
