import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from django.utils import timezone


def init_data(token: str, user_id: int) -> str:
    values = {
        "auth_date": str(int(timezone.now().timestamp())),
        "query_id": "base-tap-query",
        "user": json.dumps({"id": user_id, "first_name": "Tapper"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.mark.django_db
def test_base_tap_api_requires_auth_and_returns_batch_state(
    settings: Any, client: Any, phase4_config_factory: Any, season_factory: Any
) -> None:
    settings.TELEGRAM_BOT_TOKEN = "123:base-tap"
    now = timezone.now()
    config = phase4_config_factory(effective_at=now - timedelta(days=1))
    season_factory(config, starts_at=now - timedelta(hours=1))
    body = json.dumps({"tap_count": 10, "client_elapsed_ms": 999999})
    assert (
        client.post("/api/v1/base/taps", data=body, content_type="application/json").status_code
        == 401
    )
    response = client.post(
        "/api/v1/base/taps",
        data=body,
        content_type="application/json",
        HTTP_AUTHORIZATION=f"tma {init_data(settings.TELEGRAM_BOT_TOKEN, 555)}",
        HTTP_IDEMPOTENCY_KEY="api-batch",
    )
    assert response.status_code == 200
    assert response.json()["accepted_taps"] == 10
    assert response.json()["earned_points"] == 10
