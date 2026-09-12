import pytest

from dexo_land.api import api

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/clans/governance"),
        ("post", "/api/v1/clans/removal-cases"),
        ("post", "/api/v1/clans/governance/1/vote"),
        ("post", "/api/v1/clans/dissolution"),
        ("get", "/api/v1/clans/recovery"),
        ("post", "/api/v1/moderation/reports"),
    ],
)
def test_phase7b_routes_require_auth(client, method, path):  # type: ignore[no-untyped-def]
    response = getattr(client, method)(path, data={}, content_type="application/json")
    assert response.status_code == 401


def test_phase7b_openapi_and_no_moderator_action_surface() -> None:
    paths = api.get_openapi_schema()["paths"]
    expected = {
        "/api/v1/clans/governance",
        "/api/v1/clans/removal-cases",
        "/api/v1/clans/governance/{case_id}/vote",
        "/api/v1/clans/dissolution",
        "/api/v1/clans/recovery",
        "/api/v1/moderation/reports",
    }
    assert expected <= paths.keys()
    assert not any("actions" in path and "moderation" in path for path in paths)
