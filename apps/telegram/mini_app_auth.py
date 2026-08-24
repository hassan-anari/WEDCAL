from typing import Any

from django.conf import settings
from django.http import HttpRequest
from ninja.security import APIKeyHeader

from apps.players.models import Player
from apps.telegram.auth import TelegramAuthError, verify_init_data
from apps.telegram.services import resolve_telegram_player


class MiniAppAuth(APIKeyHeader):
    param_name = "Authorization"

    def authenticate(self, request: HttpRequest, key: str | None) -> Player | None:
        if not key or not key.startswith("tma "):
            return None
        try:
            verified = verify_init_data(
                key[4:],
                bot_token=settings.TELEGRAM_BOT_TOKEN,
                max_age_seconds=settings.TELEGRAM_INIT_DATA_MAX_AGE_SECONDS,
            )
        except TelegramAuthError:
            return None
        return resolve_telegram_player(
            verified.profile, Player.FirstSeenSource.MINI_APP, mini_app=True
        ).player

    def __call__(self, request: HttpRequest) -> Any:
        return super().__call__(request)


mini_app_auth = MiniAppAuth()
