from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from apps.missions.services import claim_mission, list_missions, start_mission, verify_mission
from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["missions"], auth=mini_app_auth)


class VerifyInput(Schema):
    open_token: str | None = None


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    return player


@router.get("", response=list[dict[str, Any]])
def missions(request: HttpRequest) -> list[dict[str, Any]]:
    return list_missions(_player(request), timezone.now())


@router.post("/{mission_key}/start", response=dict[str, Any])
def start(request: HttpRequest, mission_key: str) -> dict[str, Any]:
    result = start_mission(_player(request), mission_key, timezone.now())
    return {
        "key": mission_key,
        "version": result.progress.definition.version,
        "state": result.progress.status,
        "open_token": result.open_token,
        "destination_url": result.destination_url,
        "opened_at": result.progress.opened_at,
    }


@router.post("/{mission_key}/verify", response=dict[str, Any])
def verify(request: HttpRequest, mission_key: str, payload: VerifyInput) -> dict[str, Any]:
    row = verify_mission(_player(request), mission_key, timezone.now(), payload.open_token)
    return {
        "key": mission_key,
        "version": row.definition.version,
        "state": row.status,
        "verified_at": row.verified_at,
    }


@router.post("/{mission_key}/claim", response=dict[str, Any])
def claim(request: HttpRequest, mission_key: str) -> dict[str, Any]:
    row, created = claim_mission(_player(request), mission_key, timezone.now())
    return {
        "key": mission_key,
        "version": row.definition.version,
        "state": row.status,
        "created": created,
        "reward_amount": row.reward_amount,
        "claimed_at": row.claimed_at,
    }
