import re
import uuid
from typing import Any

import structlog
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.cache import patch_vary_headers

SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)

    @staticmethod
    def _request_id(request: HttpRequest) -> str:
        supplied = request.headers.get("X-Request-ID", "")
        return supplied if SAFE_REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex

    def __call__(self, request: HttpRequest) -> Any:
        if self.is_async:
            return self.__acall__(request)

        request_id = self._request_id(request)
        request.META["DEXO_REQUEST_ID"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response: HttpResponse = self.get_response(request)
            self._prepare_response(request, response, request_id)
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        request_id = self._request_id(request)
        request.META["DEXO_REQUEST_ID"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response: HttpResponse = await self.get_response(request)
            self._prepare_response(request, response, request_id)
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    @staticmethod
    def _prepare_response(request: HttpRequest, response: HttpResponse, request_id: str) -> None:
        response["X-Request-ID"] = request_id
        if request.path.startswith("/api/v1/base/v2/") or request.path.rstrip("/") in {
            "/api/v1/bootstrap",
            "/api/v1/auth/telegram",
        }:
            response["Cache-Control"] = "private, no-store"
            patch_vary_headers(response, ["Authorization"])


class MutationThrottleMiddleware:
    """Cheap Redis-backed request-cost guard; database domain caps remain authoritative."""

    sync_capable = True
    async_capable = False

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        policy = next(
            (
                value
                for prefix, value in settings.MUTATION_THROTTLE_POLICIES.items()
                if (request.method == "POST" or request.path.startswith("/api/v1/base/v2/"))
                and request.path.startswith(prefix)
            ),
            None,
        )
        if policy is not None:
            forwarded = (
                request.META.get("HTTP_X_FORWARDED_FOR", "").split(",", 1)[0].strip()
                if settings.TRUST_PROXY_CLIENT_IP
                else ""
            )
            identity = forwarded or request.META.get("REMOTE_ADDR", "unknown")
            bucket = int(__import__("time").time()) // policy[1]
            key = f"throttle:{request.path}:{identity}:{bucket}"
            try:
                count = 1 if cache.add(key, 1, timeout=policy[1] + 1) else cache.incr(key)
                if count > policy[0]:
                    response = JsonResponse(
                        {"detail": "Request rate limit exceeded", "code": "RATE_LIMITED"},
                        status=429,
                    )
                    response["Retry-After"] = str(policy[1])
                    return response
            except Exception:
                # Availability fail-open is safe: PostgreSQL caps still prevent over-credit.
                pass
        downstream_response: HttpResponse = self.get_response(request)
        return downstream_response
