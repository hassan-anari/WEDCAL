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
    "apps.game_config",
    "apps.seasons",
    "apps.progression",
    "apps.rewards",
    "apps.daily",
    "apps.missions",
    "apps.typing_game",
    "apps.referrals",
    "apps.network",
    "apps.base_tap",
    "apps.profile_collection",
    "apps.clans",
    "apps.moderation",
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
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
telegram_bot_id = os.getenv("TELEGRAM_BOT_ID", "").strip()
TELEGRAM_BOT_ID = int(telegram_bot_id) if telegram_bot_id else None
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS = int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "3600"))
TELEGRAM_INGRESS_RETENTION_SECONDS = int(os.getenv("TELEGRAM_INGRESS_RETENTION_SECONDS", "604800"))
TELEGRAM_INGRESS_PROCESSING_LEASE_SECONDS = int(
    os.getenv("TELEGRAM_INGRESS_PROCESSING_LEASE_SECONDS", "300")
)
TELEGRAM_INGRESS_RETRY_BASE_SECONDS = int(os.getenv("TELEGRAM_INGRESS_RETRY_BASE_SECONDS", "30"))
TELEGRAM_INGRESS_RETRY_MAX_SECONDS = int(os.getenv("TELEGRAM_INGRESS_RETRY_MAX_SECONDS", "1800"))
TELEGRAM_INGRESS_MAX_PROCESSING_ATTEMPTS = int(
    os.getenv("TELEGRAM_INGRESS_MAX_PROCESSING_ATTEMPTS", "6")
)
TELEGRAM_INGRESS_REDRIVE_BATCH_SIZE = int(os.getenv("TELEGRAM_INGRESS_REDRIVE_BATCH_SIZE", "100"))
CLAN_INVITE_CREATE_LEASE_SECONDS = int(os.getenv("CLAN_INVITE_CREATE_LEASE_SECONDS", "60"))
CLAN_INVITE_UNCERTAIN_HOLD_SECONDS = int(os.getenv("CLAN_INVITE_UNCERTAIN_HOLD_SECONDS", "300"))
CLAN_INVITE_MAX_CREATE_ATTEMPTS = int(os.getenv("CLAN_INVITE_MAX_CREATE_ATTEMPTS", "3"))
CLAN_INVITE_RETRY_BASE_SECONDS = int(os.getenv("CLAN_INVITE_RETRY_BASE_SECONDS", "30"))
CLAN_INVITE_RETRY_MAX_SECONDS = int(os.getenv("CLAN_INVITE_RETRY_MAX_SECONDS", "1800"))
CLAN_INVITE_MAX_REVOKE_ATTEMPTS = int(os.getenv("CLAN_INVITE_MAX_REVOKE_ATTEMPTS", "5"))
CLAN_INVITE_REVOKE_LEASE_SECONDS = int(os.getenv("CLAN_INVITE_REVOKE_LEASE_SECONDS", "60"))
CLAN_INVITE_REVOKE_RETRY_BASE_SECONDS = int(
    os.getenv("CLAN_INVITE_REVOKE_RETRY_BASE_SECONDS", "30")
)
CLAN_INVITE_REVOKE_RETRY_MAX_SECONDS = int(
    os.getenv("CLAN_INVITE_REVOKE_RETRY_MAX_SECONDS", "1800")
)
NETWORK_REGISTRATION_INTENT_TTL_SECONDS = int(
    os.getenv("NETWORK_REGISTRATION_INTENT_TTL_SECONDS", "900")
)
REWARD_OUTBOX_LEASE_SECONDS = int(os.getenv("REWARD_OUTBOX_LEASE_SECONDS", "300"))
REWARD_OUTBOX_RETRY_SECONDS = int(os.getenv("REWARD_OUTBOX_RETRY_SECONDS", "60"))
BASE_TAP_SETTLEMENT_LEASE_SECONDS = int(os.getenv("BASE_TAP_SETTLEMENT_LEASE_SECONDS", "300"))
BASE_TAP_SETTLEMENT_RETRY_SECONDS = int(os.getenv("BASE_TAP_SETTLEMENT_RETRY_SECONDS", "60"))

CELERY_BEAT_SCHEDULE.update(
    {
        "sweep-clan-inactivity": {
            "task": "apps.clans.tasks.sweep_clan_inactivity",
            "schedule": 3600,
        },
        "sweep-clan-bot-admin": {
            "task": "apps.clans.tasks.sweep_clan_bot_admin_deadlines",
            "schedule": 300,
        },
        "finalize-clan-activity-windows": {
            "task": "apps.clans.tasks.finalize_clan_activity_windows",
            "schedule": 60,
        },
        "finalize-clan-governance": {
            "task": "apps.clans.tasks.finalize_clan_governance",
            "schedule": 60,
        },
        "recover-reward-outbox": {
            "task": "apps.rewards.tasks.recover_reward_outbox",
            "schedule": 60,
        },
        "reconcile-pending-referrals": {
            "task": "apps.referrals.tasks.reconcile_pending_referrals",
            "schedule": 300,
        },
        "reverify-creator-groups": {
            "task": "apps.network.tasks.reverify_active_creator_groups",
            "schedule": 300,
        },
        "purge-network-evidence": {
            "task": "apps.network.tasks.purge_network_evidence",
            "schedule": 3600,
        },
        "settle-creator-windows": {
            "task": "apps.network.tasks.settle_creator_windows",
            "schedule": 60,
        },
        "recover-pending-base-taps": {
            "task": "apps.base_tap.tasks.recover_pending_base_taps",
            "schedule": 60,
        },
        "redrive-telegram-inbound-updates": {
            "task": "apps.telegram.tasks.redrive_telegram_inbound_updates",
            "schedule": 60,
        },
        "recover-clan-invite-revocations": {
            "task": "apps.clans.tasks.recover_invite_revocations",
            "schedule": 60,
        },
        "recover-clan-invite-creation": {
            "task": "apps.clans.tasks.recover_invite_creation",
            "schedule": 60,
        },
    }
)

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
