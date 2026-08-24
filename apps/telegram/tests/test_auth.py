from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from apps.telegram.auth import TelegramAuthError, verify_init_data


def test_valid_init_data_returns_verified_user(
    init_data_factory: Callable[..., str], telegram_bot_token: str
) -> None:
    result = verify_init_data(
        init_data_factory(user={"id": 123, "first_name": "Ada", "username": "ada"}),
        bot_token=telegram_bot_token,
        max_age_seconds=60,
    )
    assert result.profile.telegram_user_id == 123
    assert result.profile.username == "ada"


@pytest.mark.parametrize(
    "transform",
    [
        lambda value: value.replace("hash=", "hash=invalid"),
        lambda value: value.replace("Dexo", "Changed"),
        lambda value: value.rsplit("&hash=", 1)[0],
    ],
)
def test_invalid_tampered_or_missing_hash_is_rejected(
    init_data_factory: Callable[..., str],
    telegram_bot_token: str,
    transform: Callable[[str], str],
) -> None:
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            transform(init_data_factory()), bot_token=telegram_bot_token, max_age_seconds=60
        )


def test_expired_init_data_is_rejected(
    init_data_factory: Callable[..., str], telegram_bot_token: str
) -> None:
    with pytest.raises(TelegramAuthError, match="Expired"):
        verify_init_data(
            init_data_factory(auth_date=datetime.now(tz=UTC) - timedelta(minutes=2)),
            bot_token=telegram_bot_token,
            max_age_seconds=60,
        )


def test_malformed_user_is_rejected(
    init_data_factory: Callable[..., str], telegram_bot_token: str
) -> None:
    raw = init_data_factory(user={"first_name": "No identifier"})
    with pytest.raises(TelegramAuthError, match="Malformed Telegram user"):
        verify_init_data(raw, bot_token=telegram_bot_token, max_age_seconds=60)
