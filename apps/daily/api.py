from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router

from apps.daily.services import claim_daily, get_daily_state
from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["daily"], auth=mini_app_auth)


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    return player


def _serialize(state: Any) -> dict[str, Any]:
    return {
        "claimable": state.claimable,
        "already_claimed": not state.claimable,
        "current_streak": state.streak,
        "cycle_day": state.cycle_day,
        "reward_amount": state.reward_amount,
        "period_start": state.period_start,
        "period_end": state.period_end,
        "next_reset": state.period_end,
        "last_claim_time": state.last_claimed_at,
    }


@router.get("", response=dict[str, Any])
def state(request: HttpRequest) -> dict[str, Any]:
    return _serialize(get_daily_state(_player(request), timezone.now()))


@router.post("/claim", response=dict[str, Any])
def claim(request: HttpRequest) -> dict[str, Any]:
    row, created = claim_daily(_player(request), timezone.now())
    return {
        "created": created,
        "claim_period": row.claim_period,
        "streak": row.streak_count_after_claim,
        "cycle_day": row.cycle_day,
        "reward_amount": row.reward_amount,
        "claimed_at": row.claimed_at,
    }
