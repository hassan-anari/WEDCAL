import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from django.db import OperationalError
from django.test import Client

from apps.telegram.auth import TelegramAuthError, verify_init_data


def signed_fields(fields: dict[str, str], token: str) -> str:
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def test_valid_unicode_and_optional_signature_are_verified(telegram_bot_token: str) -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    fields = {
        "auth_date": str(int(now.timestamp())),
        "user": json.dumps({"id": 123, "first_name": "آدا + & = 🚀"}, ensure_ascii=False),
        "signature": "optional-telegram-signature",
    }
    verified = verify_init_data(
        signed_fields(fields, telegram_bot_token),
        bot_token=telegram_bot_token,
        max_age_seconds=60,
        now=now,
    )
    assert verified.profile.first_name == "آدا + & = 🚀"
    assert verified.auth_date == now


@pytest.mark.parametrize("user_id", [True, False, 1.5, "42", 0, -1, 2**63, None])
def test_signed_identity_is_not_coerced(
    user_id: object, init_data_factory: Callable[..., str], telegram_bot_token: str
) -> None:
    with pytest.raises(TelegramAuthError) as error:
        verify_init_data(
            init_data_factory(user={"id": user_id}),
            bot_token=telegram_bot_token,
            max_age_seconds=60,
        )
    assert error.value.code == "AUTH_INVALID"


@pytest.mark.parametrize("hash_value", ["آ" * 64, "0" * 63, "z" * 64, "", "0" * 65])
def test_malformed_hash_returns_auth_error(hash_value: str, telegram_bot_token: str) -> None:
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            urlencode({"auth_date": "1", "hash": hash_value}),
            bot_token=telegram_bot_token,
            max_age_seconds=60,
        )


@pytest.mark.parametrize(
    "auth_date", ["invalid", "1e100", str(2**100), str(-(2**100)), "", "1792100000"]
)
def test_signed_invalid_or_future_auth_date_is_rejected(
    auth_date: str, telegram_bot_token: str
) -> None:
    raw = signed_fields({"auth_date": auth_date, "user": '{"id":42}'}, telegram_bot_token)
    with pytest.raises(TelegramAuthError) as error:
        verify_init_data(
            raw,
            bot_token=telegram_bot_token,
            max_age_seconds=60,
            now=datetime(2026, 10, 14, tzinfo=UTC),
        )
    assert error.value.code == "AUTH_INVALID"


@pytest.mark.parametrize("user", ["[]", '"42"', "null", '{"id":42,"is_bot":true}', "{"])
def test_signed_malformed_user_is_rejected(user: str, telegram_bot_token: str) -> None:
    raw = signed_fields(
        {"auth_date": str(int(datetime.now(tz=UTC).timestamp())), "user": user},
        telegram_bot_token,
    )
    with pytest.raises(TelegramAuthError):
        verify_init_data(raw, bot_token=telegram_bot_token, max_age_seconds=60)


@pytest.mark.parametrize("field,value", [("first_name", "x" * 256), ("username", "\x00")])
def test_signed_profile_cannot_cause_database_encoding_errors(
    field: str,
    value: str,
    init_data_factory: Callable[..., str],
    telegram_bot_token: str,
) -> None:
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            init_data_factory(user={"id": 42, field: value}),
            bot_token=telegram_bot_token,
            max_age_seconds=60,
        )


def test_exact_expiry_boundary_and_replay_age(
    init_data_factory: Callable[..., str], telegram_bot_token: str
) -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    raw = init_data_factory(auth_date=now - timedelta(seconds=60))
    assert verify_init_data(
        raw, bot_token=telegram_bot_token, max_age_seconds=60, now=now
    ).auth_date == now - timedelta(seconds=60)
    with pytest.raises(TelegramAuthError) as error:
        verify_init_data(
            raw,
            bot_token=telegram_bot_token,
            max_age_seconds=60,
            now=now + timedelta(microseconds=1),
        )
    assert error.value.code == "AUTH_EXPIRED"


@pytest.mark.parametrize("suffix", ["&user=%7B%22id%22%3A999%7D", "&auth_date=1", "&hash=bad"])
def test_duplicate_fields_are_rejected(
    suffix: str, init_data_factory: Callable[..., str], telegram_bot_token: str
) -> None:
    with pytest.raises(TelegramAuthError, match="Duplicate"):
        verify_init_data(
            init_data_factory() + suffix, bot_token=telegram_bot_token, max_age_seconds=60
        )


@pytest.mark.parametrize(
    "path",
    ["/bootstrap", "/rewards/history", "/profile/overview", "/notifications", "/arena/state"],
)
@pytest.mark.parametrize(
    "header,code",
    [
        (None, "AUTH_MISSING"),
        ("", "AUTH_MISSING"),
        ("Bearer 42", "AUTH_INVALID"),
        ("tma telegram_user_id=42", "AUTH_INVALID"),
        ("tma auth_date=1&hash=" + "آ" * 64, "AUTH_INVALID"),
    ],
)
def test_initial_endpoints_reject_unsigned_credentials_without_provisioning(
    client: Client, settings: Any, path: str, header: str | None, code: str
) -> None:
    settings.TELEGRAM_BOT_TOKEN = "local-fixture-token"
    headers: dict[str, Any] = {"HTTP_AUTHORIZATION": header} if header is not None else {}
    with patch("apps.telegram.mini_app_auth.resolve_telegram_player") as resolve:
        response = client.get(
            "/api/v1" + path + "?telegram_user_id=42&player_id=42&initDataUnsafe=42",
            HTTP_X_REQUEST_ID="b1-auth-check",
            HTTP_X_FORWARDED_FOR="42",
            **headers,
        )
    resolve.assert_not_called()
    assert response.status_code == 401
    assert response.json()["code"] == code
    assert response.json()["request_id"] == response["X-Request-ID"] == "b1-auth-check"
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("transport", ["header", "body"])
def test_expired_credentials_have_same_error_on_both_transports(
    client: Client,
    settings: Any,
    init_data_factory: Callable[..., str],
    telegram_bot_token: str,
    transport: str,
) -> None:
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    raw = init_data_factory(auth_date=datetime.now(tz=UTC) - timedelta(days=1))
    with patch("apps.telegram.mini_app_auth.resolve_telegram_player") as resolve:
        response = (
            client.get("/api/v1/bootstrap", HTTP_AUTHORIZATION=f"tma {raw}")
            if transport == "header"
            else client.post(
                "/api/v1/auth/telegram", {"init_data": raw}, content_type="application/json"
            )
        )
    resolve.assert_not_called()
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_EXPIRED"
    assert raw not in response.content.decode()
    assert "outside Telegram" not in response.json()["detail"]


@pytest.mark.parametrize("transport", ["header", "body"])
def test_server_auth_configuration_failure_is_not_invalid_user(
    client: Client, settings: Any, transport: str
) -> None:
    settings.TELEGRAM_BOT_TOKEN = ""
    response = (
        client.get("/api/v1/bootstrap", HTTP_AUTHORIZATION="tma raw")
        if transport == "header"
        else client.post(
            "/api/v1/auth/telegram", {"init_data": "raw"}, content_type="application/json"
        )
    )
    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_UNAVAILABLE"


@pytest.mark.parametrize("body,status", [("{", 400), ("{}", 422), ('{"init_data":42}', 422)])
def test_auth_request_errors_are_typed(client: Client, body: str, status: int) -> None:
    response = client.post("/api/v1/auth/telegram", body, content_type="application/json")
    assert response.status_code == status
    assert response.json()["code"] == "REQUEST_INVALID"
    assert response.json()["request_id"] == response["X-Request-ID"]


def test_provisioning_outage_is_sanitized(
    client: Client, settings: Any, init_data_factory: Callable[..., str], telegram_bot_token: str
) -> None:
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    with patch(
        "apps.telegram.mini_app_auth.resolve_telegram_player",
        side_effect=OperationalError("private database address and password"),
    ):
        response = client.get("/api/v1/bootstrap", HTTP_AUTHORIZATION=f"tma {init_data_factory()}")
    assert response.status_code == 503
    assert response.json()["code"] == "INITIALIZATION_UNAVAILABLE"
    assert "private database" not in response.content.decode()
