from typing import Annotated, Any, Literal
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema
from pydantic import Field

from apps.core.b3 import B3Error, b3_responses, canonical_state
from apps.core.periods import resolve_daily_period
from apps.game_config.services import resolve_effective_game_config
from apps.players.models import Player
from apps.rewards.models import RewardLedger
from apps.rewards.reveals import confirmed_reward
from apps.telegram.mini_app_auth import mini_app_auth
from apps.typing_game.calculations import display_score
from apps.typing_game.models import TypingAttempt
from apps.typing_game.schemas import (
    AttemptOutput,
    HistoryOutput,
    SubmitOutput,
    TypingClaimOutput,
    TypingOutput,
)
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
        "score": display_score(attempt.accuracy_bps, attempt.wpm),
        "accuracy_bps": attempt.accuracy_bps,
        "wpm": attempt.wpm,
        "reward_amount": attempt.reward_amount,
    }


@router.post("/claim", response=b3_responses(TypingClaimOutput))
@transaction.atomic
def claim(request: HttpRequest) -> Any:
    Player.objects.select_for_update().get(pk=_player(request).pk)
    before = canonical_state(_player(request), timezone.now())
    try:
        row, created = claim_wallet(
            _player(request), request.headers.get("Idempotency-Key", ""), timezone.now()
        )
    except B3Error:
        raise
    except (TypingUnavailable, ValueError) as exc:
        raise B3Error("CLAIM_UNAVAILABLE") from exc
    after = canonical_state(_player(request), timezone.now())
    return {
        "reward": confirmed_reward(
            rows=list(
                RewardLedger.objects.filter(
                    player=_player(request),
                    source_type="TYPING_CLAIM",
                    metadata__typing_claim_id=row.pk,
                ).order_by("pk")
            ),
            event_id=f"typing:{row.pk}",
            source="TYPING_GAME",
            created=created,
            before=before,
            after=after,
        ),
        "created": created,
        "amount": row.amount,
        "claimed_at": row.claimed_at,
        "typing": state(request),
        "canonical": after,
    }


@router.get("/history", response=b3_responses(HistoryOutput))
def history(
    request: HttpRequest,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    period: Literal["all", "current"] = "all",
) -> dict[str, Any]:
    window = None
    if period == "current":
        at = timezone.now()
        config = resolve_effective_game_config(at)
        window = resolve_daily_period(at, config.typing.reset_timezone)
    rows = TypingAttempt.objects.filter(player=_player(request), submitted_at__isnull=False)
    if window:
        rows = rows.filter(reset_period=window.key)
    if cursor is not None:
        if window:
            anchor = (
                TypingAttempt.objects.filter(
                    player=_player(request), pk=cursor, submitted_at__isnull=False
                )
                .values_list("reset_period", flat=True)
                .first()
            )
            if anchor is None:
                raise B3Error("REQUEST_INVALID", 422)
            if anchor != window.key:
                raise B3Error("TYPING_PERIOD_CHANGED", 409)
        rows = rows.filter(pk__lt=cursor)
    batch = list(
        rows.select_related("season", "config_version", "wallet_credit").order_by("-pk")[
            : limit + 1
        ]
    )
    return {
        "period": period,
        "period_key": window.key if window else None,
        "period_start": window.start if window else None,
        "period_end": window.end if window else None,
        "items": [
            {
                "attempt_id": row.public_id,
                "status": row.status,
                "started_at": row.started_at,
                "submitted_at": row.submitted_at,
                "reset_period": row.reset_period,
                "score": display_score(row.accuracy_bps, row.wpm),
                "accuracy_bps": row.accuracy_bps,
                "wpm": row.wpm,
                "reward_amount": row.reward_amount,
                "season": row.season.code,
                "config_version": row.config_version.version,
                "claimed": bool(
                    getattr(row, "wallet_credit", None) and row.wallet_credit.claimed_by_id
                ),
            }
            for row in batch[:limit]
        ],
        "next_cursor": batch[limit - 1].pk if len(batch) > limit else None,
    }
