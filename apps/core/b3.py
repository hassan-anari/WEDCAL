"""Shared B3 read contract; economic state is read under the canonical player lock."""

from datetime import datetime
from typing import Any, Literal

from django.http import HttpRequest
from ninja import Schema

from apps.base_tap.v2 import state_snapshot
from apps.core.api_errors import IntegrationErrorOutput
from apps.players.models import Player
from apps.rewards.schemas import CanonicalState as CanonicalState


class B3Error(ValueError):
    def __init__(self, code: str, status: int = 409, *, retry_after: int | None = None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)


class B3ErrorOutput(Schema):
    code: str
    detail: str
    retryable: bool
    retry_after_seconds: int | None = None


def canonical_state(player: Player, at: datetime) -> dict[str, Any]:
    snapshot = state_snapshot(player, at=at)
    return {key: snapshot[key] for key in CanonicalState.model_fields}


def authenticated_player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    return player


class ThrottleOutput(Schema):
    code: Literal["RATE_LIMITED"]
    detail: str


def b3_responses(success: type[Schema]) -> dict[int, Any]:
    return {
        200: success,
        401: IntegrationErrorOutput,
        403: B3ErrorOutput,
        404: B3ErrorOutput,
        409: B3ErrorOutput,
        422: B3ErrorOutput,
        429: B3ErrorOutput | ThrottleOutput,
        503: B3ErrorOutput | IntegrationErrorOutput,
    }
