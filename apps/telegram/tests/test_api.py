from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from django.test import Client

from apps.players.models import Player
from apps.telegram.models import TelegramInboundUpdate


@pytest.mark.django_db
def test_mini_app_endpoint_creates_and_reuses_player(
    client: Client,
    settings: Any,
    init_data_factory: Callable[..., str],
    telegram_bot_token: str,
) -> None:
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    raw = init_data_factory(user={"id": 100, "first_name": "First"})
    first = client.post(
        "/api/v1/auth/telegram", {"init_data": raw}, content_type="application/json"
    )
    second = client.post(
        "/api/v1/auth/telegram", {"init_data": raw}, content_type="application/json"
    )
    assert first.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    player = Player.objects.get(telegram_user_id=100)
    assert player.first_seen_source == Player.FirstSeenSource.MINI_APP
    assert player.first_mini_app_at is not None


@pytest.mark.django_db
def test_mini_app_endpoint_rejects_invalid_auth(client: Client, settings: Any) -> None:
    settings.TELEGRAM_BOT_TOKEN = "token"
    response = client.post(
        "/api/v1/auth/telegram",
        {"init_data": "auth_date=1&hash=bad"},
        content_type="application/json",
    )
    assert response.status_code == 401
    assert Player.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("secret", [None, "wrong"])
def test_webhook_rejects_missing_or_invalid_secret_without_state(
    webhook_client: Client, secret: str | None
) -> None:
    headers: dict[str, Any] = {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": secret} if secret else {}
    response = webhook_client.post(
        "/api/v1/telegram/webhook",
        {"update_id": 1},
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 401
    assert TelegramInboundUpdate.objects.count() == 0


@pytest.mark.django_db
def test_webhook_rejects_malformed_json_and_update_id(webhook_client: Client) -> None:
    headers: dict[str, Any] = {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "webhook-test-secret"}
    malformed = webhook_client.post(
        "/api/v1/telegram/webhook", "{", content_type="application/json", **headers
    )
    invalid = webhook_client.post(
        "/api/v1/telegram/webhook", {"update_id": "1"}, content_type="application/json", **headers
    )
    assert malformed.status_code == 400
    assert invalid.status_code == 400
    assert TelegramInboundUpdate.objects.count() == 0


@pytest.mark.django_db
def test_webhook_persists_before_dispatch_and_deduplicates(webhook_client: Client) -> None:
    headers: dict[str, Any] = {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "webhook-test-secret"}

    def assert_committed(inbound: TelegramInboundUpdate) -> None:
        assert TelegramInboundUpdate.objects.filter(pk=inbound.pk).exists()

    with patch(
        "apps.telegram.api.dispatch_inbound_update", side_effect=assert_committed
    ) as dispatch:
        first = webhook_client.post(
            "/api/v1/telegram/webhook", {"update_id": 5}, content_type="application/json", **headers
        )
        second = webhook_client.post(
            "/api/v1/telegram/webhook", {"update_id": 5}, content_type="application/json", **headers
        )
    assert first.status_code == second.status_code == 200
    assert TelegramInboundUpdate.objects.filter(update_id=5).count() == 1
    assert (
        dispatch.call_count == 2
    )  # Pending retries may republish; worker processing is idempotent.


@pytest.mark.django_db
def test_broker_failure_leaves_durable_update_for_telegram_retry(webhook_client: Client) -> None:
    headers: dict[str, Any] = {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "webhook-test-secret"}
    with patch("apps.telegram.api.dispatch_inbound_update", side_effect=ConnectionError("broker")):
        failed = webhook_client.post(
            "/api/v1/telegram/webhook", {"update_id": 8}, content_type="application/json", **headers
        )
    assert failed.status_code == 503
    assert TelegramInboundUpdate.objects.filter(update_id=8).count() == 1
    with patch("apps.telegram.api.dispatch_inbound_update") as redispatch:
        retried = webhook_client.post(
            "/api/v1/telegram/webhook", {"update_id": 8}, content_type="application/json", **headers
        )
    assert retried.status_code == 200
    redispatch.assert_called_once()


@pytest.mark.django_db
def test_processed_duplicate_acks_without_dispatch(webhook_client: Client) -> None:
    TelegramInboundUpdate.objects.create(
        update_id=9,
        payload={"update_id": 9},
        payload_expires_at="2026-08-25T00:00:00Z",
        state=TelegramInboundUpdate.State.PROCESSED,
    )
    with patch("apps.telegram.api.dispatch_inbound_update") as dispatch:
        response = webhook_client.post(
            "/api/v1/telegram/webhook",
            {"update_id": 9},
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="webhook-test-secret",
        )
    assert response.status_code == 200
    dispatch.assert_not_called()
