from datetime import datetime
from typing import Literal

from ninja import Schema


class BootstrapPlayer(Schema):
    id: str
    telegram_user_id: int
    first_name: str
    last_name: str
    username: str
    language_code: str
    photo_url: str


class BootstrapProgression(Schema):
    lifetime_points: int
    level: int
    current_threshold: int
    next_threshold: int | None
    points_into_level: int
    points_remaining: int | None
    dexo_stage: str


class BootstrapSeason(Schema):
    code: str
    title: str
    status: str
    starts_at: datetime
    ends_at: datetime
    season_points: int


class LevelRuleOutput(Schema):
    level: int
    min_lifetime_points: int


class DexoStageOutput(Schema):
    key: str
    min_level: int


class BootstrapProgressionPolicy(Schema):
    levels: list[LevelRuleOutput]
    dexo_stages: list[DexoStageOutput]
    clan_min_level: int | None


class BootstrapConfig(Schema):
    version: str
    effective_at: datetime
    schema_version: int
    payload_checksum: str
    progression: BootstrapProgressionPolicy


class BootstrapDaily(Schema):
    claimable: bool
    streak: int
    cycle_day: int
    reward_amount: int
    next_reset: datetime


class BootstrapTyping(Schema):
    attempts_used: int
    attempts_remaining: int
    claimable_balance: int


class BootstrapMissions(Schema):
    available_count: int
    reward_ready_count: int
    unclaimed_count: int


class BootstrapReferral(Schema):
    status: str | None
    verified_invites: int


class BootstrapNetwork(Schema):
    group_count: int
    active_group_count: int


class BootstrapFeatures(Schema):
    season: bool
    daily: bool
    typing: bool
    missions: bool
    referral: bool
    network: bool
    base_tap: bool
    clan: bool
    profile: bool
    arena: bool


class BootstrapGameplay(Schema):
    moderation_state: Literal["ACTIVE", "FLAGGED", "SUSPENDED", "BANNED"]
    reward_earning_allowed: bool


class BootstrapOutput(Schema):
    contract_version: Literal["1.1"]
    request_id: str
    server_time: datetime
    player: BootstrapPlayer
    progression: BootstrapProgression
    season: BootstrapSeason | None
    config: BootstrapConfig
    features: BootstrapFeatures
    gameplay: BootstrapGameplay
    daily: BootstrapDaily | None
    typing: BootstrapTyping | None
    missions: BootstrapMissions | None
    referral: BootstrapReferral | None
    network: BootstrapNetwork | None
