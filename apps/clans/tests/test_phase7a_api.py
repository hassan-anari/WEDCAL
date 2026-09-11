from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext


@pytest.mark.django_db
def test_clan_api_requires_mini_app_auth_and_openapi_path(
    clan_world: Any, init_data_factory: Any, settings: Any
) -> None:
    _, _, _, _, _, clan = clan_world
    clan()
    client = Client()
    assert client.get("/api/v1/clans/").status_code == 401
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    header = {
        "HTTP_AUTHORIZATION": f"tma {init_data_factory(user={'id': 79999, 'first_name': 'API'})}"
    }
    response = client.get("/api/v1/clans/", headers={"Authorization": header["HTTP_AUTHORIZATION"]})
    assert response.status_code == 200
    schema = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/clans/" in schema["paths"]
    assert "/api/v1/clans/invite/current" in schema["paths"]


@pytest.mark.django_db
def test_discovery_member_counts_have_bounded_queries(
    clan_world: Any, init_data_factory: Any, settings: Any
) -> None:
    _, _, _, _, _, clan = clan_world
    for _ in range(2):
        clan()
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    client = Client()
    header = {
        "HTTP_AUTHORIZATION": "tma "
        + init_data_factory(user={"id": 79998, "first_name": "Queries"})
    }

    def discover() -> tuple[int, list[dict[str, object]]]:
        with CaptureQueriesContext(connection) as captured:
            response = client.get(
                "/api/v1/clans/?limit=100",
                headers={"Authorization": header["HTTP_AUTHORIZATION"]},
            )
        assert response.status_code == 200
        return len(captured), response.json()

    # Resolve the authenticated Player before either measured request.
    assert (
        client.get(
            "/api/v1/clans/?limit=100",
            headers={"Authorization": header["HTTP_AUTHORIZATION"]},
        ).status_code
        == 200
    )

    small_query_count, small_rows = discover()
    for _ in range(6):
        clan()
    large_query_count, large_rows = discover()

    assert len(small_rows) == 2
    assert len(large_rows) == 8
    assert {row["member_count"] for row in large_rows} == {1}
    # Auth/config/Season overhead is fixed; six more Clans must not add six COUNT queries.
    assert large_query_count <= small_query_count + 1


@pytest.mark.django_db
def test_invite_polling_before_retry_time_does_not_repeat_telegram_create(
    clan_world: Any, init_data_factory: Any, settings: Any, monkeypatch: Any
) -> None:
    from apps.clans.models import ClanRole
    from apps.clans.services import decide_request, request_join
    from apps.telegram.bot_api import TelegramResult

    _, _, _, resolved, player, clan = clan_world
    soldier, clan_season = player(ClanRole.SOLDIER), clan()
    request = request_join(soldier, clan_season, resolved)
    decide_request(clan_season.captain, request.pk, accept=True)
    calls: list[int] = []

    def rate_limited(*_args: object) -> TelegramResult:
        calls.append(1)
        return TelegramResult(False, category="rate_limited", retry_after=120)

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", rate_limited)
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    authorization = "tma " + init_data_factory(
        user={"id": soldier.telegram_user_id, "first_name": "Soldier"}
    )
    client = Client()
    for _ in range(4):
        response = client.get(
            "/api/v1/clans/invite/current",
            headers={"Authorization": authorization},
        )
        assert response.status_code == 200
        assert response.json()["state"] == "FAILED"
    request.invite.refresh_from_db()
    assert calls == [1]
    assert request.invite.attempt_count == 1


@pytest.mark.django_db
def test_clan_api_normal_missing_state_errors_are_safe(
    clan_world: Any, init_data_factory: Any, settings: Any
) -> None:
    _, _, season, _, player, _ = clan_world
    soldier = player()
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    authorization = "tma " + init_data_factory(
        user={"id": soldier.telegram_user_id, "first_name": "Soldier"}
    )
    client = Client()
    headers = {"Authorization": authorization}
    missing = client.post(
        "/api/v1/clans/join-requests/999999/decision",
        data={"accept": True},
        content_type="application/json",
        headers=headers,
    )
    assert missing.status_code == 404
    assert client.post("/api/v1/clans/leave", headers=headers).status_code == 204
    season.status = "CLOSED"
    season.save(update_fields=("status",))
    unavailable = client.get("/api/v1/clans/", headers=headers)
    assert unavailable.status_code == 409
    assert unavailable.json() == {"detail": "No current Clan Season is available"}
