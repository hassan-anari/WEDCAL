from datetime import datetime

from ninja import Schema


class ReferralTierOutput(Schema):
    key: str
    min_verified_referrals: int
    share_bps: int


class ReferralEligibility(Schema):
    level: int
    required_level: int
    required_account_age_hours: int
    eligible: bool


class ReferralOutput(Schema):
    code: str
    link: str | None
    pending_count: int
    verified_count: int
    current_tier: str | None
    current_share_bps: int
    next_tier: ReferralTierOutput | None
    tiers: list[ReferralTierOutput]
    enabled: bool
    earned_points: int
    attribution_status: str | None
    activation: ReferralEligibility | None
    inviter_bonus: int
    invitee_bonus: int


class InviteeOutput(Schema):
    photo_url: str = ""
    name: str
    status: str
    attributed_at: datetime
    verified_at: datetime | None
    activation: ReferralEligibility


class InviteePage(Schema):
    items: list[InviteeOutput]
    next_offset: int | None
