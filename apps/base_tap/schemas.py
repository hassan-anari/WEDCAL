from datetime import datetime
from typing import Literal
from uuid import UUID

from ninja import Schema
from pydantic import ConfigDict, Field

from apps.rewards.schemas import BootstrapProgression, BootstrapProgressionPolicy


class LeaseInput(Schema):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    takeover: bool = Field(default=False, strict=True)
    expected_generation: int | None = Field(default=None, ge=0, strict=True)


class BatchInput(Schema):
    model_config = ConfigDict(extra="forbid")
    lease_id: UUID
    session_id: UUID
    generation: int = Field(ge=1, strict=True)
    sequence_start: int = Field(ge=1, le=1001, strict=True)
    tap_count: int = Field(ge=1, le=1000, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")


class LeaseOutput(Schema):
    lease_id: UUID
    session_id: UUID
    generation: int
    authorized_allowance: int
    reward_per_tap: int
    cooldown_seconds: int = Field(ge=1)
    max_taps_per_batch: int
    min_batch_interval_ms: int
    expires_at: datetime
    sequence_start: int
    sequence_end: int
    config_version: str
    season_id: str


class SessionOwnership(Schema):
    status: Literal["AVAILABLE", "OWNED", "ANOTHER_DEVICE", "SESSION_MOVED"]
    active_until: datetime | None
    explicit_switch_required: bool


class ReleaseInput(Schema):
    model_config = ConfigDict(extra="forbid")
    lease_id: UUID
    session_id: UUID
    generation: int = Field(ge=1, strict=True)


class TapStateOutput(Schema):
    contract_version: Literal["2.0"] = "2.0"
    server_time: datetime
    player_id: str
    season_id: str | None
    config_version: str
    issuance_enabled: bool
    reward_earning_allowed: bool
    revision: int
    generation: int
    receipt_cursor: int
    burst_remaining: int
    cooldown_until: datetime | None
    lease: LeaseOutput | None
    progression: BootstrapProgression
    season_points: int
    progression_policy: BootstrapProgressionPolicy
    ownership: SessionOwnership | None = None


class TapReceiptOutput(Schema):
    """Immutable ledger evidence; independent of the response's current snapshot."""

    receipt_id: int
    idempotency_key: str
    lease_id: UUID
    session_id: UUID
    generation: int
    sequence_start: int
    requested_taps: int
    sequence_through: int
    accepted_taps: int
    rejected_taps: int
    earned_points: int
    accepted_at: datetime
    season_id: str
    config_version: str


class BatchOutput(Schema):
    duplicate: bool
    accepted_taps: int
    rejected_taps: int
    earned_points: int
    receipt_id: int
    sequence_through: int
    snapshot: TapStateOutput
    receipt: TapReceiptOutput


class TapErrorOutput(Schema):
    code: Literal[
        "AUTH_MISSING",
        "AUTH_INVALID",
        "AUTH_EXPIRED",
        "AUTH_UNAVAILABLE",
        "PLAYER_RESTRICTED",
        "LEASE_UNKNOWN",
        "ACTIVE_SESSION",
        "GENERATION_STALE",
        "LEASE_REVOKED",
        "LEASE_EXPIRED",
        "ALLOWANCE_EXHAUSTED",
        "SEQUENCE_REPLAY",
        "IDEMPOTENCY_CONFLICT",
        "SEASON_CHANGED",
        "SEASON_UNAVAILABLE",
        "SEASON_FINALIZED",
        "EPOCH_FINALIZED",
        "COOLDOWN",
        "SEQUENCE_GAP",
        "REQUEST_INVALID",
        "BATCH_TOO_LARGE",
        "RATE_LIMITED",
        "ISSUANCE_DISABLED",
        "LEGACY_DRAIN_REQUIRED",
        "CONFIG_UNAVAILABLE",
        "TEMPORARY_UNAVAILABLE",
    ]
    detail: str
    retryable: bool
    retry_after_ms: int | None = None
    snapshot: TapStateOutput | None = None
