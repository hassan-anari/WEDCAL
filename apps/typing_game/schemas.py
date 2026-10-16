from datetime import datetime
from uuid import UUID

from ninja import Schema

from apps.core.b3 import CanonicalState
from apps.rewards.reveals import ConfirmedReward


class AttemptOutput(Schema):
    created: bool
    attempt_id: UUID
    phrase_key: str
    phrase_version: int
    phrase_text: str
    started_at: datetime
    expires_at: datetime
    challenge_seconds: int


class TypingOutput(Schema):
    attempts_used: int
    attempts_remaining: int
    attempt_limit: int
    next_reset: datetime
    claimable_balance: int
    best_wpm: int
    best_accuracy_bps: int
    active_attempt: str | None
    active_attempt_detail: AttemptOutput | None


class SubmitOutput(Schema):
    score: int | None
    created: bool
    attempt_id: UUID
    status: str
    accuracy_bps: int | None
    wpm: int | None
    reward_amount: int
    typing: TypingOutput


class TypingClaimOutput(Schema):
    reward: ConfirmedReward | None
    created: bool
    amount: int
    claimed_at: datetime
    typing: TypingOutput
    canonical: CanonicalState


class RunOutput(Schema):
    score: int | None
    attempt_id: UUID
    status: str
    started_at: datetime
    submitted_at: datetime
    accuracy_bps: int | None
    wpm: int | None
    reward_amount: int
    season: str
    config_version: str
    claimed: bool


class HistoryOutput(Schema):
    items: list[RunOutput]
    next_cursor: int | None
