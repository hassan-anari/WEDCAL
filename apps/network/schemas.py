from datetime import datetime
from uuid import UUID

from ninja import Schema

from apps.core.b3 import CanonicalState


class NetworkSummary(Schema):
    canonical: CanonicalState
    enabled: bool
    active_groups: int
    group_count: int
    max_active_groups: int
    can_register: bool
    speaker_rewards_enabled: bool
    earned_points: int
    settlement_window_seconds: int


class CreatorGroupOutput(Schema):
    public_id: UUID
    title: str
    photo_url: str = ""
    state: str
    bot_admin: bool
    broadcast_opted_in: bool
    creator_verified_at: datetime | None
    bot_verified_at: datetime | None
    next_verification_at: datetime
    verification_retrying: bool
    earned_points: int
    qualified_characters: int
    unique_speakers: int
    last_activity_at: datetime | None


class CreatorGroupPage(Schema):
    items: list[CreatorGroupOutput]
    next_offset: int | None


class IntentOutput(Schema):
    startgroup_url: str
    expires_at: datetime
