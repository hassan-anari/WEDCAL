import asyncio
from unittest.mock import patch

import pytest
import structlog
from django.http import HttpRequest, HttpResponse
from django.test import AsyncClient, Client

from apps.core.health import readiness_status
from apps.core.middleware import RequestContextMiddleware


def test_liveness_is_independent_and_deterministic(client: Client) -> None:
    with patch("apps.core.views.readiness_status") as readiness:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    readiness.assert_not_called()


def test_readiness_success(client: Client) -> None:
    with patch(
        "apps.core.views.readiness_status",
        return_value=(True, {"postgresql": "ok", "redis": "ok", "rabbitmq": "ok"}),
    ):
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.parametrize("failed_dependency", ["postgresql", "redis", "rabbitmq"])
def test_readiness_dependency_failure_is_safe(client: Client, failed_dependency: str) -> None:
    statuses = {"postgresql": "ok", "redis": "ok", "rabbitmq": "ok"}
    statuses[failed_dependency] = "unavailable"
    with patch("apps.core.views.readiness_status", return_value=(False, statuses)):
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"][failed_dependency] == "unavailable"


def test_dependency_exception_details_are_not_returned() -> None:
    def exposes_secret() -> None:
        raise RuntimeError("postgres://user:secret@example.invalid/database")

    is_ready, statuses = readiness_status({"postgresql": exposes_secret})
    assert not is_ready
    assert statuses == {"postgresql": "unavailable"}
    assert "secret" not in str(statuses)


def test_request_id_is_bounded(client: Client) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "x" * 1000})
    assert len(response.headers["X-Request-ID"]) == 32


def test_request_context_is_cleared_when_sync_handler_raises() -> None:
    request = HttpRequest()

    def failing_handler(request: HttpRequest) -> HttpResponse:
        assert structlog.contextvars.get_contextvars()["request_id"]
        raise RuntimeError("downstream failure")

    middleware = RequestContextMiddleware(failing_handler)
    with pytest.raises(RuntimeError, match="downstream failure"):
        middleware(request)
    assert structlog.contextvars.get_contextvars() == {}


def test_request_context_supports_async_handler_and_cleans_up() -> None:
    request = HttpRequest()
    request.META["HTTP_X_REQUEST_ID"] = "safe-async-request-id"

    async def async_handler(request: HttpRequest) -> HttpResponse:
        assert structlog.contextvars.get_contextvars()["request_id"] == "safe-async-request-id"
        return HttpResponse()

    middleware = RequestContextMiddleware(async_handler)
    response = asyncio.run(middleware(request))
    assert response.headers["X-Request-ID"] == "safe-async-request-id"
    assert structlog.contextvars.get_contextvars() == {}


def test_request_context_middleware_preserves_async_request_stack() -> None:
    async def request_live() -> None:
        response = await AsyncClient().get(
            "/health/live", headers={"X-Request-ID": "async-stack-request"}
        )
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == "async-stack-request"

    asyncio.run(request_live())


@pytest.mark.django_db
def test_real_readiness_integration(client: Client) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"postgresql": "ok", "redis": "ok", "rabbitmq": "ok"},
    }
