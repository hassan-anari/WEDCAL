from datetime import datetime
from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema

from apps.game_config.services import resolve_effective_game_config
from apps.players.models import Player
from apps.progression.services import refresh_player_progression
from apps.rewards.models import RewardLedger
from apps.seasons.models import PlayerSeason
from apps.seasons.services import resolve_current_season
from apps.telegram.mini_app_auth import mini_app_auth

bootstrap_router = Router(tags=["bootstrap"], auth=mini_app_auth)
rewards_router = Router(tags=["rewards"], auth=mini_app_auth)


class HistoryQuery(Schema):
    cursor: int | None = None
    limit: int = 20


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


@rewards_router.get("/history", response=HistoryOutput)
def reward_history(request: HttpRequest, query: Query[HistoryQuery]) -> dict[str, Any]:
    limit = min(max(query.limit, 1), 100)
    rewards = RewardLedger.objects.filter(player=_player(request)).order_by("-id")
    if query.cursor is not None:
        rewards = rewards.filter(id__lt=query.cursor)
    rows = list(
        rewards.values("id", "source_type", "context", "player_amount", "occurred_at")[: limit + 1]
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "next_cursor": rows[-1]["id"] if has_more else None}


@bootstrap_router.get("", response=dict[str, Any])
def bootstrap(request: HttpRequest) -> dict[str, Any]:
    now = timezone.now()
    player = _player(request)
    resolved = resolve_effective_game_config(now)
    progression, result = refresh_player_progression(player, resolved)
    season = resolve_current_season(at=now, required=False)
    season_data: dict[str, Any] | None = None
    if season:
        player_season = (
            PlayerSeason.objects.filter(player=player, season=season).only("season_points").first()
        )
        season_data = {
            "code": season.code,
            "title": season.title,
            "status": season.status,
            "starts_at": season.starts_at,
            "ends_at": season.ends_at,
            "season_points": player_season.season_points if player_season else 0,
        }
    return {
        "player": {
            "telegram_user_id": player.telegram_user_id,
            "first_name": player.first_name,
            "last_name": player.last_name,
            "username": player.username,
        },
        "progression": {
            "lifetime_points": progression.lifetime_points,
            "level": result.level,
            "current_threshold": result.current_threshold,
            "next_threshold": result.next_threshold,
            "points_into_level": result.points_into_level,
            "points_remaining": result.points_remaining,
            "dexo_stage": result.dexo_stage,
        },
        "season": season_data,
        "config": {"version": resolved.version, "effective_at": resolved.effective_at},
    }
