import re
import uuid
from typing import Any

import structlog
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponse

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
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response: HttpResponse = self.get_response(request)
            response["X-Request-ID"] = request_id
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        request_id = self._request_id(request)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response: HttpResponse = await self.get_response(request)
            response["X-Request-ID"] = request_id
            return response
        finally:
            structlog.contextvars.clear_contextvars()
