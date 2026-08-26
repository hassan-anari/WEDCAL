from typing import Any
from uuid import UUID

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth
from apps.typing_game.services import claim_wallet, get_typing_state, start_attempt, submit_attempt

router = Router(tags=["typing"], auth=mini_app_auth)


class Submission(Schema):
    typed_text: str


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    return player


@router.get("", response=dict[str, Any])
def state(request: HttpRequest) -> dict[str, Any]:
    value = get_typing_state(_player(request), timezone.now())
    return {
        "attempts_used": value.attempts_used,
        "attempts_remaining": value.attempts_remaining,
        "attempt_limit": value.attempt_limit,
        "next_reset": value.next_reset,
        "claimable_balance": value.claimable_balance,
        "best_wpm": value.best_wpm,
        "best_accuracy_bps": value.best_accuracy_bps,
        "active_attempt": str(value.active_attempt.public_id) if value.active_attempt else None,
    }


@router.post("/attempts/start", response=dict[str, Any])
def start(request: HttpRequest) -> dict[str, Any]:
    attempt, created = start_attempt(
        _player(request), request.headers.get("Idempotency-Key", ""), timezone.now()
    )
    return {
        "created": created,
        "attempt_id": attempt.public_id,
        "phrase_key": attempt.phrase.key,
        "phrase_version": attempt.phrase.version,
        "phrase_text": attempt.phrase.text,
        "started_at": attempt.started_at,
        "expires_at": attempt.expires_at,
        "challenge_seconds": int((attempt.expires_at - attempt.started_at).total_seconds()),
    }


@router.post("/attempts/{attempt_id}/submit", response=dict[str, Any])
def submit(request: HttpRequest, attempt_id: UUID, payload: Submission) -> dict[str, Any]:
    attempt, created = submit_attempt(
        _player(request), attempt_id, payload.typed_text, timezone.now()
    )
    return {
        "created": created,
        "attempt_id": attempt.public_id,
        "status": attempt.status,
        "accuracy_bps": attempt.accuracy_bps,
        "wpm": attempt.wpm,
        "reward_amount": attempt.reward_amount,
    }


@router.post("/claim", response=dict[str, Any])
def claim(request: HttpRequest) -> dict[str, Any]:
    row, created = claim_wallet(
        _player(request), request.headers.get("Idempotency-Key", ""), timezone.now()
    )
    return {"created": created, "amount": row.amount, "claimed_at": row.claimed_at}
