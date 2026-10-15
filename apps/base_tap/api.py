from typing import Any
from uuid import UUID

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.base_tap import v2
from apps.base_tap.schemas import (
    BatchInput,
    BatchOutput,
    LeaseInput,
    ReleaseInput,
    TapErrorOutput,
    TapStateOutput,
)
from apps.base_tap.services import submit_tap_batch
from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["base"], auth=mini_app_auth)


class TapBatchSubmission(Schema):
    tap_count: int


@router.post("/taps", response=dict[str, object])
def submit(request: HttpRequest, payload: TapBatchSubmission) -> dict[str, object]:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    try:
        result = submit_tap_batch(
            player,
            tap_count=payload.tap_count,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            at=timezone.now(),
        )
    except (ValueError, LookupError) as exc:
        if not settings.BASE_TAP_LEGACY_INGRESS_ENABLED:
            raise HttpError(409, "Legacy Tap ingress paused; use Tap v2 after cutover") from exc
        raise HttpError(400, "Invalid Base Tap submission") from exc
    return {
        "created": result.created,
        "accepted_taps": result.accepted_taps,
        "rejected_taps": result.rejected_taps,
        "earned_points": result.earned_points,
        "pending_points": result.pending_points,
        "daily_accepted_taps": result.daily_accepted_taps,
        "daily_remaining_taps": result.daily_remaining_taps,
        "settlement_status": result.settlement_status,
    }


def authenticated_player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    return player


V2_RESPONSES = {
    200: TapStateOutput,
    401: TapErrorOutput,
    422: TapErrorOutput,
    403: TapErrorOutput,
    409: TapErrorOutput,
    429: TapErrorOutput,
    503: TapErrorOutput,
}


@router.get("/v2/tap-state", response=V2_RESPONSES)
def tap_state(request: HttpRequest, session_id: UUID | None = None) -> Any:
    return v2.state_snapshot(authenticated_player(request), at=timezone.now(), session=session_id)


@router.post("/v2/tap-leases", response=V2_RESPONSES)
def tap_lease(request: HttpRequest, payload: LeaseInput) -> Any:
    return v2.issue_lease(
        authenticated_player(request),
        session=payload.session_id,
        at=timezone.now(),
        takeover=payload.takeover,
        expected_generation=payload.expected_generation,
    )


@router.post("/v2/tap-release", response=V2_RESPONSES)
def tap_release(request: HttpRequest, payload: ReleaseInput) -> Any:
    return v2.release_lease(
        authenticated_player(request),
        lease_id=payload.lease_id,
        session=payload.session_id,
        generation=payload.generation,
        at=timezone.now(),
    )


@router.post(
    "/v2/tap-batches",
    response={**V2_RESPONSES, 200: BatchOutput, 404: TapErrorOutput, 422: TapErrorOutput},
)
def tap_batch(request: HttpRequest, payload: BatchInput) -> Any:
    return v2.submit_batch(authenticated_player(request), payload, at=timezone.now())
