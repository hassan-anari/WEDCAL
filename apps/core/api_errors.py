from typing import Literal

from ninja import Schema
from ninja.errors import HttpError

ErrorCode = Literal[
    "AUTH_MISSING",
    "AUTH_INVALID",
    "AUTH_EXPIRED",
    "AUTH_UNAVAILABLE",
    "INITIALIZATION_UNAVAILABLE",
    "REQUEST_INVALID",
]


class IntegrationErrorOutput(Schema):
    detail: str
    code: ErrorCode
    request_id: str


class IntegrationError(HttpError):
    def __init__(self, status_code: int, code: ErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(status_code, detail)
