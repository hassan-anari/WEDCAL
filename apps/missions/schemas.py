from datetime import datetime
from typing import Literal

from ninja import Schema

from apps.core.b3 import CanonicalState
from apps.rewards.reveals import ConfirmedReward


class MissionOutput(Schema):
    key: str
    version: int
    title: str
    description: str
    verification_family: Literal["INTERNAL", "TELEGRAM", "TIMED_EXTERNAL"]
    repeat_policy: str
    reward_amount: int
    state: Literal["AVAILABLE", "IN_PROGRESS", "REWARD_READY", "CLAIMED", "UNAVAILABLE"]
    available: bool
    unavailable_reason: str | None
    destination_url: str | None
    opened_at: datetime | None
    verified_at: datetime | None
    claimed_at: datetime | None


class MissionStartOutput(Schema):
    key: str
    version: int
    state: str
    open_token: str | None
    destination_url: str | None
    opened_at: datetime | None


class MissionVerifyOutput(Schema):
    key: str
    version: int
    state: str
    verified_at: datetime | None


class MissionClaimOutput(Schema):
    reward: ConfirmedReward | None
    key: str
    version: int
    state: str
    created: bool
    reward_amount: int
    claimed_at: datetime | None
    canonical: CanonicalState
