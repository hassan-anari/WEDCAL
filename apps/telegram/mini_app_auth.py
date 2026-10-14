from django.conf import settings
from django.db import InterfaceError, OperationalError
from django.http import HttpRequest
from ninja.security import APIKeyHeader

from apps.core.api_errors import IntegrationError
from apps.players.models import Player
from apps.players.services import PlayerResolution
from apps.telegram.auth import TelegramAuthError, VerifiedInitData, verify_init_data
from apps.telegram.services import resolve_telegram_player


class MiniAppAuth(APIKeyHeader):
    param_name = "Authorization"

    def authenticate(self, request: HttpRequest, key: str | None) -> Player:
        if not key or not key.strip():
            raise IntegrationError(401, "AUTH_MISSING", "Telegram authentication is required")
        parts = key.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "tma":
            raise IntegrationError(401, "AUTH_INVALID", "Invalid Telegram authentication")
        try:
            verified = verify_init_data(
                parts[1].strip(),
                bot_token=settings.TELEGRAM_BOT_TOKEN,
                max_age_seconds=settings.TELEGRAM_INIT_DATA_MAX_AGE_SECONDS,
            )
        except TelegramAuthError as exc:
            raise authentication_error(exc) from exc
        return provision_mini_app_player(verified).player


def authentication_error(exc: TelegramAuthError) -> IntegrationError:
    detail = {
        "AUTH_MISSING": "Telegram authentication is required",
        "AUTH_INVALID": "Invalid Telegram authentication",
        "AUTH_EXPIRED": "Telegram authentication has expired. Reopen the Mini App.",
        "AUTH_UNAVAILABLE": "Telegram authentication is temporarily unavailable",
    }[exc.code]
    return IntegrationError(503 if exc.code == "AUTH_UNAVAILABLE" else 401, exc.code, detail)


def provision_mini_app_player(verified: VerifiedInitData) -> PlayerResolution:
    try:
        return resolve_telegram_player(
            verified.profile, Player.FirstSeenSource.MINI_APP, mini_app=True
        )
    except (OperationalError, InterfaceError) as exc:
        raise IntegrationError(
            503, "INITIALIZATION_UNAVAILABLE", "Player initialization is temporarily unavailable"
        ) from exc


mini_app_auth = MiniAppAuth()
