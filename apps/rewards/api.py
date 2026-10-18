from datetime import datetime
from typing import Any, Literal

from django.core.exceptions import ValidationError
from django.db import InterfaceError, OperationalError
from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema

from apps.core.api_errors import IntegrationError, IntegrationErrorOutput
from apps.core.b3 import CanonicalState, b3_responses, canonical_state
from apps.game_config.services import GameConfigUnavailable
from apps.players.models import Player
from apps.rewards.bootstrap import build_bootstrap
from apps.rewards.models import RewardLedger
from apps.rewards.schemas import BootstrapOutput
from apps.seasons.services import NoActiveSeason, SeasonNotEligible
from apps.telegram.mini_app_auth import mini_app_auth

bootstrap_router = Router(tags=["bootstrap"], auth=mini_app_auth)
rewards_router = Router(tags=["rewards"], auth=mini_app_auth)


class HistoryQuery(Schema):
    cursor: int | None = None
    limit: int = 20
    category: Literal["network"] | None = None


class HistoryItem(Schema):
    id: int
    source_type: str
    context: str
    player_amount: int
    occurred_at: datetime


class HistoryOutput(Schema):
    items: list[HistoryItem]
    next_cursor: int | None


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    if not isinstance(player, Player):
        raise RuntimeError("Mini App authentication did not resolve a player")
    return player


@rewards_router.get("/history", response=b3_responses(HistoryOutput))
def reward_history(request: HttpRequest, query: Query[HistoryQuery]) -> dict[str, Any]:
    limit = min(max(query.limit, 1), 100)
    rewards = RewardLedger.objects.filter(player=_player(request)).order_by("-id")
    if query.category == "network":
        rewards = rewards.filter(source_type__in=("CREATOR", "NETWORK_SPEAKER"))
    if query.cursor is not None:
        rewards = rewards.filter(id__lt=query.cursor)
    rows = list(
        rewards.values("id", "source_type", "context", "player_amount", "occurred_at")[: limit + 1]
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "next_cursor": rows[-1]["id"] if has_more else None}


@bootstrap_router.get(
    "", response={200: BootstrapOutput, 401: IntegrationErrorOutput, 503: IntegrationErrorOutput}
)
def bootstrap(request: HttpRequest) -> dict[str, Any]:
    try:
        return build_bootstrap(
            _player(request), timezone.now(), request_id=request.META.get("DEXO_REQUEST_ID", "")
        )
    except (
        GameConfigUnavailable,
        ValidationError,
        NoActiveSeason,
        SeasonNotEligible,
        OperationalError,
        InterfaceError,
    ) as exc:
        raise IntegrationError(
            503,
            "INITIALIZATION_UNAVAILABLE",
            "Application initialization is temporarily unavailable",
        ) from exc


@rewards_router.get("/state", response=b3_responses(CanonicalState))
def progression_state(request: HttpRequest) -> dict[str, Any]:
    return canonical_state(_player(request), timezone.now())
