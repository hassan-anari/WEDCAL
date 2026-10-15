from typing import Any
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema
from pydantic import Field

from apps.core.b3 import B3Error, b3_responses, canonical_state
from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth
from apps.typing_game.models import TypingAttempt
from apps.typing_game.schemas import AttemptOutput, SubmitOutput, TypingClaimOutput, TypingOutput
from apps.typing_game.services import (
    TypingQuotaExceeded,
    TypingUnavailable,
    claim_wallet,
    get_typing_state,
    start_attempt,
    submit_attempt,
)

router = Router(tags=["typing"], auth=mini_app_auth)


class Submission(Schema):
    typed_text: str = Field(max_length=4096)


class ErrorResponse(Schema):
    detail: str
    code: str


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    return player


def serialize_attempt(attempt: TypingAttempt, created: bool = False) -> dict[str, Any]:
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


@router.get("", response=b3_responses(TypingOutput))
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
        "active_attempt_detail": serialize_attempt(value.active_attempt)
        if value.active_attempt
        else None,
    }


@router.post("/attempts/start", response=b3_responses(AttemptOutput))
def start(request: HttpRequest) -> Any:
    try:
        attempt, created = start_attempt(
            _player(request), request.headers.get("Idempotency-Key", ""), timezone.now()
        )
    except B3Error:
        raise
    except ValueError as exc:
        if not isinstance(exc, TypingQuotaExceeded):
            raise B3Error("IDEMPOTENCY_KEY_REQUIRED", 422) from exc
        raise B3Error("QUOTA_EXHAUSTED", 429) from exc
    except TypingUnavailable as exc:
        raise B3Error("TYPING_UNAVAILABLE") from exc
    return serialize_attempt(attempt, created)


@router.post("/attempts/{attempt_id}/submit", response=b3_responses(SubmitOutput))
@transaction.atomic
def submit(request: HttpRequest, attempt_id: UUID, payload: Submission) -> dict[str, Any]:
    try:
        attempt, created = submit_attempt(
            _player(request), attempt_id, payload.typed_text, timezone.now()
        )
    except TypingAttempt.DoesNotExist as exc:
        raise B3Error("TYPING_ATTEMPT_NOT_FOUND", 404) from exc
    except ValueError as exc:
        raise B3Error("TYPING_SUBMISSION_INVALID", 422) from exc
    return {
        "created": created,
        "attempt_id": attempt.public_id,
        "status": attempt.status,
        "typing": state(request),
        "accuracy_bps": attempt.accuracy_bps,
        "wpm": attempt.wpm,
        "reward_amount": attempt.reward_amount,
    }


@router.post("/claim", response=b3_responses(TypingClaimOutput))
@transaction.atomic
def claim(request: HttpRequest) -> Any:
    try:
        row, created = claim_wallet(
            _player(request), request.headers.get("Idempotency-Key", ""), timezone.now()
        )
    except B3Error:
        raise
    except (TypingUnavailable, ValueError) as exc:
        raise B3Error("CLAIM_UNAVAILABLE") from exc
    return {
        "created": created,
        "amount": row.amount,
        "claimed_at": row.claimed_at,
        "typing": state(request),
        "canonical": canonical_state(_player(request), timezone.now()),
    }
