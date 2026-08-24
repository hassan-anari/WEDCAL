import os

from django.core.exceptions import ImproperlyConfigured

from dexo_land.settings.base import *  # noqa: F403


def require_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"Required production setting {name} is missing")
    return value


SECRET_KEY = require_environment("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = [
    host.strip() for host in require_environment("DJANGO_ALLOWED_HOSTS").split(",") if host.strip()
]
DATABASES["default"].update(  # noqa: F405
    {
        "NAME": require_environment("POSTGRES_DB"),
        "USER": require_environment("POSTGRES_USER"),
        "PASSWORD": require_environment("POSTGRES_PASSWORD"),
        "HOST": require_environment("POSTGRES_HOST"),
        "PORT": int(require_environment("POSTGRES_PORT")),
    }
)
REDIS_URL = require_environment("REDIS_URL")
CACHES["default"]["LOCATION"] = REDIS_URL  # noqa: F405
CELERY_BROKER_URL = require_environment("CELERY_BROKER_URL")
TELEGRAM_BOT_TOKEN = require_environment("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = require_environment("TELEGRAM_WEBHOOK_SECRET")
DEBUG = False
LOG_JSON = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
