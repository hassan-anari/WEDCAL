from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

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
        raise HttpError(400, "Invalid Base Tap submission") from exc
    batch = result.batch
    return {
        "created": result.created,
        "accepted_taps": batch.accepted_taps,
        "rejected_taps": batch.rejected_taps,
        "earned_points": batch.earned_points,
        "pending_points": result.pending_points,
        "daily_accepted_taps": result.daily_accepted_taps,
        "daily_remaining_taps": result.daily_remaining_taps,
        "settlement_status": batch.status,
    }
