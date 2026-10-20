"""Versioned Season 1 DLX policy. Loading a policy never enables Production settlement."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError

CATEGORIES = (
    "CLAN_PERFORMANCE",
    "CORE_GAMEPLAY",
    "CREATOR_COMMUNITY_GROWTH",
    "VERIFIED_REFERRAL",
    "LOYALTY_CONSISTENCY",
)
INDEPENDENT_MISSION_KEYS = frozenset(
    {"visit-dalexio", "view-x-update", "join-telegram", "read-season-brief"}
)

# Monetary inputs have exactly one primary category. Loyalty uses distinct actor/day presence.
SOURCE_COMPONENTS = {
    "BASE_TAP_BATCH": ("CORE_GAMEPLAY", "tap"),
    "BASE_TAP_V2_BATCH": ("CORE_GAMEPLAY", "tap"),
    "TYPING_CREDIT": ("CORE_GAMEPLAY", "typing"),
    "DAILY_CLAIM": ("CORE_GAMEPLAY", "daily"),
    "PERSONAL_MISSION_VERIFIED": ("CORE_GAMEPLAY", "mission"),
    "CLAN_CONTRIBUTION": ("CLAN_PERFORMANCE", "contribution"),
    "CREATOR_ACTIVITY": ("CREATOR_COMMUNITY_GROWTH", "speaker"),
    "REFERRAL_ACTIVATION": ("VERIFIED_REFERRAL", "acquisition"),
    "COMMUNITY_ACTIVITY": ("LOYALTY_CONSISTENCY", "presence"),
}


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class EconomicPolicy:
    values: dict[str, Any]

    def number(self, key: str) -> int:
        value = self.values.get(key)
        if type(value) is not int or value <= 0:
            raise ValidationError(f"Invalid economic policy value: {key}")
        return value

    def target(self, category: str) -> int:
        value = self.values["participation_targets"].get(category)
        if type(value) is not int or value <= 0:
            raise ValidationError("Invalid participation target")
        return value

    def validate(self) -> None:
        if self.values.get("implementation") not in {"S1_FINALIZED_V1", "CONFIGURABLE_V1"}:
            raise ValidationError("ECONOMIC_POLICY_IMPLEMENTATION_UNSUPPORTED")
        for key in (
            "scale",
            "days",
            "min_age_hours",
            "meaningful_days",
            "daily_only_periods",
            "community_messages_per_day",
            "tap_weight",
            "tap_saturation",
            "typing_weight",
            "typing_runs_per_day",
            "typing_wpm_ceiling",
            "daily_weight",
            "mission_weight",
            "mission_limit",
            "clan_work_weight",
            "clan_consistency_weight",
            "clan_saturation",
            "clan_meaningful_contribution",
            "creator_h_weight",
            "creator_w_weight",
            "creator_h_saturation",
            "creator_window_speakers",
            "creator_window_characters",
            "creator_windows_per_day",
            "creator_speaker_span_seconds",
            "creator_window_seconds",
            "creator_distinct_messages",
            "referral_saturation",
            "minimum_category_score",
            "player_cap_bps",
            "carry_release_bps",
            "correction_reserve_bps",
        ):
            self.number(key)
        for category in CATEGORIES:
            self.target(category)
        s = self.number("scale")
        if (
            s != 10000
            or (self.values["implementation"] == "S1_FINALIZED_V1" and self.number("days") != 7)
            or sum(
                self.number(k)
                for k in ("tap_weight", "typing_weight", "daily_weight", "mission_weight")
            )
            != s
            or self.number("clan_work_weight") + self.number("clan_consistency_weight") != s
            or self.number("creator_h_weight") + self.number("creator_w_weight") != s
            or any(
                self.number(k) > s
                for k in ("player_cap_bps", "carry_release_bps", "correction_reserve_bps")
            )
            or (
                self.values["implementation"] == "S1_FINALIZED_V1"
                and self.values.get("growth_milestones") != []
            )
            or self.values.get("recognition") != "FINALIZED_ONCE_V1"
        ):
            raise ValidationError("Invalid Season 1 economic policy")
        if self.values["implementation"] == "CONFIGURABLE_V1":
            if "growth_milestones" in self.values:
                raise ValidationError("Growth milestones belong only to GameConfig")
            growth = self.values.get("growth")
            if not isinstance(growth, dict) or growth.get("rule") not in {
                "DISABLED",
                "SEASON_TO_DATE_PRE_FREEZE_EQUAL_V1",
            }:
                raise ValidationError("Unsupported Growth recognition/placement rule")
            if (
                growth["rule"] != "DISABLED"
                and not str(growth.get("approval_reference", "")).strip()
            ):
                raise ValidationError("Growth rule requires explicit owner approval")
            if self.number("meaningful_days") > self.number("days"):
                raise ValidationError("Qualification days exceed Epoch duration")
        missions = self.values.get("independent_mission_keys")
        if not isinstance(missions, list) or len(missions) != len(set(missions)):
            raise ValidationError("Invalid independent Mission allowlist")
        if not set(missions) <= INDEPENDENT_MISSION_KEYS:
            raise ValidationError("Mission allowlist requires supported independent verifiers")


def parse_policy(values: dict[str, Any]) -> EconomicPolicy:
    policy = EconomicPolicy(values)
    policy.validate()
    return policy
