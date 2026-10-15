from typing import Any

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from apps.core.b3 import B3Error, b3_responses, canonical_state
from apps.missions.schemas import (
    MissionClaimOutput,
    MissionOutput,
    MissionStartOutput,
    MissionVerifyOutput,
)
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


@router.get("", response={**b3_responses(MissionOutput), 200: list[MissionOutput]})
def missions(request: HttpRequest) -> list[dict[str, Any]]:
    return list_missions(_player(request), timezone.now())


@router.post("/{mission_key}/start", response=b3_responses(MissionStartOutput))
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


@router.post("/{mission_key}/verify", response=b3_responses(MissionVerifyOutput))
def verify(request: HttpRequest, mission_key: str, payload: VerifyInput) -> dict[str, Any]:
    try:
        row = verify_mission(_player(request), mission_key, timezone.now(), payload.open_token)
    except B3Error:
        raise
    except ValueError as exc:
        raise B3Error("MISSION_VERIFICATION_INVALID", 422) from exc
    return {
        "key": mission_key,
        "version": row.definition.version,
        "state": row.status,
        "verified_at": row.verified_at,
    }


@router.post("/{mission_key}/claim", response=b3_responses(MissionClaimOutput))
@transaction.atomic
def claim(request: HttpRequest, mission_key: str) -> dict[str, Any]:
    row, created = claim_mission(_player(request), mission_key, timezone.now())
    return {
        "key": mission_key,
        "version": row.definition.version,
        "state": row.status,
        "canonical": canonical_state(_player(request), timezone.now()),
        "created": created,
        "reward_amount": row.reward_amount,
        "claimed_at": row.claimed_at,
    }
