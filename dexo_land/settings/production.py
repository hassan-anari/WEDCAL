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
DEBUG = False
LOG_JSON = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
