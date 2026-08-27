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
    daily_data = None
    typing_data = None
    missions_data = None
    if season and resolved.daily.enabled:
        from apps.daily.services import get_daily_state

        daily = get_daily_state(player, now, resolved)
        daily_data = {
            "claimable": daily.claimable,
            "streak": daily.streak,
            "cycle_day": daily.cycle_day,
            "reward_amount": daily.reward_amount,
            "next_reset": daily.period_end,
        }
    if season and resolved.typing.enabled:
        from apps.typing_game.services import get_typing_state

        typing = get_typing_state(player, now)
        typing_data = {
            "attempts_used": typing.attempts_used,
            "attempts_remaining": typing.attempts_remaining,
            "claimable_balance": typing.claimable_balance,
        }
    if season and resolved.missions.enabled:
        from apps.missions.models import PlayerMission
        from apps.missions.services import list_missions

        missions = list_missions(player, now)
        ready = PlayerMission.objects.filter(
            player=player, status=PlayerMission.Status.REWARD_READY
        ).count()
        missions_data = {
            "available_count": sum(item["state"] == "AVAILABLE" for item in missions),
            "reward_ready_count": ready,
            "unclaimed_count": ready,
        }
    referral_data = None
    network_data = None
    if resolved.referral.enabled:
        from apps.referrals.models import ReferralRelation

        relation = ReferralRelation.objects.filter(invitee=player).first()
        referral_data = {
            "status": relation.status if relation else None,
            "verified_invites": ReferralRelation.objects.filter(
                inviter=player, status=ReferralRelation.Status.VERIFIED
            ).count(),
        }
    if resolved.network.enabled:
        from apps.network.models import CreatorGroupRegistration

        groups = CreatorGroupRegistration.objects.filter(creator=player)
        network_data = {
            "group_count": groups.count(),
            "active_group_count": groups.filter(
                state=CreatorGroupRegistration.State.ACTIVE
            ).count(),
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
        "daily": daily_data,
        "typing": typing_data,
        "missions": missions_data,
        "referral": referral_data,
        "network": network_data,
    }
