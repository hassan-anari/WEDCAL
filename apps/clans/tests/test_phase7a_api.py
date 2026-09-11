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
    for _ in range(6):
        clan()
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    client = Client()
    header = {
        "HTTP_AUTHORIZATION": "tma "
        + init_data_factory(user={"id": 79998, "first_name": "Queries"})
    }
    # Auth/config/season overhead is fixed; adding Clans must not add one COUNT per row.
    with CaptureQueriesContext(connection) as captured:
        response = client.get(
            "/api/v1/clans/?limit=100",
            headers={"Authorization": header["HTTP_AUTHORIZATION"]},
        )
    assert response.status_code == 200 and len(response.json()) == 6
    assert len(captured) <= 12
