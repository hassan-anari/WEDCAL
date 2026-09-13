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
OPERATIONS_METRICS_TOKEN = require_environment("OPERATIONS_METRICS_TOKEN")
DEBUG = False
LOG_JSON = True
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in require_environment("DJANGO_CSRF_TRUSTED_ORIGINS").split(",")
    if origin.strip()
]
if os.getenv("DJANGO_TRUST_X_FORWARDED_PROTO", "false").lower() in {"1", "true", "yes"}:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv("DJANGO_MAX_REQUEST_BODY_BYTES", "1048576"))
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE
