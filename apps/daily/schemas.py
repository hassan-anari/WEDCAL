from datetime import datetime

from ninja import Schema

from apps.core.b3 import CanonicalState
from apps.rewards.reveals import ConfirmedReward


class DailyOutput(Schema):
    claimable: bool
    already_claimed: bool
    current_streak: int
    cycle_day: int
    reward_amount: int
    reward_cycle: list[int]
    period_start: datetime
    period_end: datetime
    next_reset: datetime
    last_claim_time: datetime | None


class DailyClaimOutput(Schema):
    reward: ConfirmedReward | None
    created: bool
    claim_period: str
    streak: int
    cycle_day: int
    reward_amount: int
    claimed_at: datetime
    daily: DailyOutput
    canonical: CanonicalState
