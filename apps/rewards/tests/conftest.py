import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from django.test import Client


@pytest.fixture
def telegram_bot_token() -> str:
    return "123456:test-token"


@pytest.fixture
def init_data_factory(telegram_bot_token: str):  # type: ignore[no-untyped-def]
    def make(
        *,
        user: dict[str, object] | None = None,
        auth_date: datetime | None = None,
        token: str = telegram_bot_token,
    ) -> str:
        values = {
            "auth_date": str(int((auth_date or datetime.now(tz=UTC)).timestamp())),
            "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
            "user": json.dumps(user or {"id": 42, "first_name": "Dexo"}, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(values)

    return make


@pytest.fixture
def webhook_client(settings, client: Client) -> Client:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_WEBHOOK_SECRET = "webhook-test-secret"
    return client
