import logging
import os
from pathlib import Path
from typing import Any

import structlog

BASE_DIR = Path(__file__).resolve().parents[2]


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key-not-for-production")
DEBUG = env_bool("DEBUG")
ALLOWED_HOSTS = [
    item.strip()
    for item in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost").split(",")
    if item.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core",
    "apps.players",
    "apps.telegram",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.core.middleware.RequestContextMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "dexo_land.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "dexo_land.wsgi.application"
ASGI_APPLICATION = "dexo_land.asgi.application"

db_options: dict[str, Any] = {}
if env_bool("DB_POOL_ENABLED"):
    db_options["pool"] = {
        "min_size": int(os.getenv("DB_POOL_MIN_SIZE", "1")),
        "max_size": int(os.getenv("DB_POOL_MAX_SIZE", "10")),
    }
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "dexo_land"),
        "USER": os.getenv("POSTGRES_USER", "dexo_land"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "dexo_land_local"),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": int(os.getenv("POSTGRES_PORT", "5432")),
        "CONN_MAX_AGE": 0,
        "OPTIONS": db_options,
    }
}

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHES = {"default": {"BACKEND": "django_redis.cache.RedisCache", "LOCATION": REDIS_URL}}

CELERY_BROKER_URL = os.getenv(
    "CELERY_BROKER_URL", "amqp://dexo_land:dexo_land_local@localhost:5672//"
)
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
TELEGRAM_INGRESS_PURGE_INTERVAL_SECONDS = int(
    os.getenv("TELEGRAM_INGRESS_PURGE_INTERVAL_SECONDS", "3600")
)
CELERY_BEAT_SCHEDULE: dict[str, Any] = {
    "purge-expired-telegram-payloads": {
        "task": "apps.telegram.tasks.purge_expired_telegram_payloads",
        "schedule": TELEGRAM_INGRESS_PURGE_INTERVAL_SECONDS,
    }
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
telegram_bot_id = os.getenv("TELEGRAM_BOT_ID", "").strip()
TELEGRAM_BOT_ID = int(telegram_bot_id) if telegram_bot_id else None
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS = int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "3600"))
TELEGRAM_INGRESS_RETENTION_SECONDS = int(os.getenv("TELEGRAM_INGRESS_RETENTION_SECONDS", "604800"))

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_JSON = env_bool("LOG_JSON", os.getenv("DJANGO_SETTINGS_MODULE", "").endswith(".production"))
shared_processors: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]
renderer = structlog.processors.JSONRenderer() if LOG_JSON else structlog.dev.ConsoleRenderer()
structlog.configure(
    processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": renderer,
            "foreign_pre_chain": shared_processors,
        }
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "structured"}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
}
logging.captureWarnings(True)
