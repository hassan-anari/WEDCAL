import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.game_config.models import GameConfigVersion

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LevelRule:
    level: int
    min_lifetime_points: int


@dataclass(frozen=True, slots=True)
class DexoStageRule:
    key: str
    min_level: int


@dataclass(frozen=True, slots=True)
class ProgressionConfig:
    levels: tuple[LevelRule, ...]
    dexo_stages: tuple[DexoStageRule, ...]


@dataclass(frozen=True, slots=True)
class DailyConfig:
    enabled: bool
    reset_timezone: str
    reset_hour: int
    reset_minute: int
    reward_cycle_points: tuple[int, ...]
    missed_full_period_resets_streak: bool
    catch_up_claims_enabled: bool


@dataclass(frozen=True, slots=True)
class MissionRule:
    key: str
    verification: str
    reward_points: int
    repeat: str


@dataclass(frozen=True, slots=True)
class MissionsConfig:
    enabled: bool
    verification_families: tuple[str, ...]
    timed_external_min_dwell_seconds: int
    economy_review_required_above_points: int
    definitions: tuple[MissionRule, ...]

    def definition(self, key: str) -> MissionRule:
        try:
            return next(rule for rule in self.definitions if rule.key == key)
        except StopIteration as exc:
            raise ValidationError("Mission is absent from effective economy config") from exc


@dataclass(frozen=True, slots=True)
class TypingBracket:
    threshold: int
    points: int


@dataclass(frozen=True, slots=True)
class TypingConfig:
    enabled: bool
    attempts_per_day: int
    reset_timezone: str
    challenge_seconds: int
    require_full_phrase_completion: bool
    min_accuracy_bps: int
    min_wpm: int
    accuracy_brackets: tuple[TypingBracket, ...]
    wpm_brackets: tuple[TypingBracket, ...]
    max_reward_per_attempt: int
    suspicious_wpm: int
    reject_wpm: int


@dataclass(frozen=True, slots=True)
class ReferralTier:
    key: str
    min_verified_referrals: int
    share_bps: int


@dataclass(frozen=True, slots=True)
class ReferralConfig:
    enabled: bool
    min_level: int
    min_account_age_hours: int
    anti_sybil_pass_required: bool
    blocked_moderation_states: tuple[str, ...]
    inviter_bonus: int
    invitee_bonus: int
    tiers: tuple[ReferralTier, ...]
    eligible_reward_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    enabled: bool
    min_message_chars: int
    max_rewardable_chars: int
    max_messages_per_minute: int
    speaker_enabled: bool
    speaker_points_per_unit: int
    window_seconds: int
    chars_per_point: int
    min_unique_speakers: int
    max_points_per_window: int
    max_points_per_creator_day: int
    max_active_groups: int
    duplicate_window_seconds: int
    normalized_duplicate_window_seconds: int
    suspicious_count: int
    reject_count: int


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    message_content_days: int
    fingerprint_days: int
    moderation_evidence_after_close_days: int


@dataclass(frozen=True, slots=True)
class BaseTapConfig:
    enabled: bool
    points_per_rewarded_tap: int
    rewarded_taps_per_day: int
    reset_timezone: str
    max_taps_per_batch: int
    min_batch_interval_ms: int
    receipt_retention_days: int


@dataclass(frozen=True, slots=True)
class GrowthMilestone:
    key: str
    qualified_player_count: int
    unlock_dlx_amount: int


@dataclass(frozen=True, slots=True)
class DlxDistributionConfig:
    product_period_days: int
    epoch_count: int
    epoch_days: int
    closing_days: int
    default_settlement_start_epoch: int
    base_pool_dlx_amount: int
    max_pool_dlx_amount: int
    category_weights_bps: tuple[tuple[str, int], ...]
    qualified_player_predicate: dict[str, Any]
    growth_milestones: tuple[GrowthMilestone, ...]


@dataclass(frozen=True, slots=True)
class AchievementRule:
    key: str
    available_s1: bool
    target: int


@dataclass(frozen=True, slots=True)
class CollectionRule:
    key: str
    unlock: str


@dataclass(frozen=True, slots=True)
class MedalRule:
    key: str
    threshold: int | None


@dataclass(frozen=True, slots=True)
class ProfileCollectionConfig:
    achievements: tuple[AchievementRule, ...]
    titles: tuple[CollectionRule, ...]
    frames: tuple[CollectionRule, ...]


@dataclass(frozen=True, slots=True)
class SeasonHonorsConfig:
    medals: tuple[MedalRule, ...]


@dataclass(frozen=True, slots=True)
class ClanConfig:
    enabled: bool
    min_level: int
    base_capacity: int
    leave_cooldown_hours: int
    max_voluntary_leaves_per_season: int
    access_rejoin_grace_hours: int
    soldier_warning_days: int
    soldier_removal_days: int
    captain_warning_days: int
    captain_recovery_days: int
    governance_vote_hours: int
    governance_quorum_bps: int
    governance_approval_bps: int
    governance_minimum_voters: int
    bot_admin_restore_grace_hours: int
    hq_curve: tuple["ClanHQRule", ...]
    max_capacity_including_items: int
    activity: "ClanActivityConfig"
    anti_fraud: "ActivityFraudConfig"
    treasury_split_bps: int
    contribution: "ClanContributionConfig"
    captain_bonus: "CaptainBonusConfig"
    mission_bands: tuple["ClanMissionBand", ...]
    inventory_stacking_rule: str
    inventory_items: tuple["ClanItemConfig", ...]
    united_front_threshold: int
    treasury_keeper_threshold: int

    def item(self, item_id: str) -> "ClanItemConfig":
        try:
            return next(item for item in self.inventory_items if item.item_id == item_id)
        except StopIteration as exc:
            raise ValidationError("Clan item is absent from effective config") from exc


@dataclass(frozen=True, slots=True)
class ClanHQRule:
    level: int
    min_xp: int
    base_capacity: int


@dataclass(frozen=True, slots=True)
class ClanActivityConfig:
    context: str
    min_message_chars: int
    max_rewardable_chars: int
    max_messages_per_minute: int
    chars_per_gross_point: int
    window_seconds: int
    max_gross_points_per_window: int


@dataclass(frozen=True, slots=True)
class ActivityFraudConfig:
    exact_duplicate_window_seconds: int
    near_duplicate_window_seconds: int
    suspicious_count: int
    reject_count: int


@dataclass(frozen=True, slots=True)
class ClanContributionConfig:
    gross_numerator: int
    gross_denominator: int
    units_per_hq_xp: int
    score_numerator: int
    score_denominator: int


@dataclass(frozen=True, slots=True)
class CaptainBonusConfig:
    enabled: bool
    share_bps: int
    daily_cap: int


@dataclass(frozen=True, slots=True)
class ClanMissionBand:
    key: str
    participant_gross_points: int
    direct_hq_xp: int


@dataclass(frozen=True, slots=True)
class ClanItemEffect:
    category: str
    value: int


@dataclass(frozen=True, slots=True)
class ClanItemConfig:
    item_id: str
    name: str
    available_s1: bool
    price: int
    activation: str | None
    duration_minutes: int | None
    effects: tuple[ClanItemEffect, ...]
    max_uses_per_season: int | None


@dataclass(frozen=True, slots=True)
class ResolvedGameConfig:
    id: int
    version: str
    effective_at: datetime
    progression: ProgressionConfig
    daily: DailyConfig
    missions: MissionsConfig
    typing: TypingConfig
    referral: ReferralConfig
    network: NetworkConfig
    retention: RetentionConfig
    base_tap: BaseTapConfig
    dlx_distribution: DlxDistributionConfig | None
    profile_collection: ProfileCollectionConfig | None
    season_honors: SeasonHonorsConfig | None
    clan: ClanConfig | None


def parse_clan(payload: dict[str, Any]) -> ClanConfig:
    """Parse the complete, integer-only Phase 8 Clan policy."""
    try:
        raw = _config_object(payload["clan"], "clan")
        unlock = _config_object(raw["unlock"], "clan.unlock")
        capacity = _config_object(raw["capacity"], "clan.capacity")
        curve = capacity["hq_curve"]
        leave = _config_object(raw["leave"], "clan.leave")
        recovery = _config_object(raw["telegram_recovery"], "clan.telegram_recovery")
        inactivity = _config_object(raw["inactivity"], "clan.inactivity")
        governance = _config_object(raw["governance"], "clan.governance")
        activity_raw = _config_object(raw["activity"], "clan.activity")
        fraud = _config_object(payload["anti_fraud"]["clan"], "anti_fraud.clan")
        treasury = _config_object(raw["treasury"], "clan.treasury")
        contribution = _config_object(raw["contribution"], "clan.contribution")
        bonus = _config_object(raw["captain_bonus"], "clan.captain_bonus")
        mission_bands_raw = _config_object(raw["missions"]["reward_bands"], "clan missions")
        inventory = _config_object(raw["inventory"], "clan.inventory")
        achievements = _config_object(raw["achievements"], "clan.achievements")
        if not isinstance(curve, list) or not curve:
            raise TypeError
        base = _config_object(curve[0], "clan.capacity.hq_curve[0]")
        if _integer(base["hq_level"], "clan initial hq_level", minimum=1) != 1:
            raise ValidationError("Clan base capacity must come from HQ level 1")
        hq_curve = tuple(
            ClanHQRule(
                _integer(x["hq_level"], "hq level", minimum=1),
                _integer(x["min_cumulative_hq_xp"], "hq threshold"),
                _integer(x["base_capacity"], "hq capacity", minimum=1),
            )
            for x in curve
        )
        supported_effects = {
            "HQ_XP_RATE_BONUS_BPS",
            "CONTRIBUTION_RATE_BONUS_BPS",
            "CLAN_MISSION_PROGRESS_BONUS_BPS",
            "SEASON_CAPACITY_ADD",
            "HQ_XP_GRANT",
        }
        items = []
        for item_raw in inventory["items"]:
            item = _config_object(item_raw, "clan item")
            effects = tuple(
                ClanItemEffect(
                    str(effect["type"]), _integer(effect["value"], "item effect", minimum=1)
                )
                for effect in item.get("effects", [])
            )
            if any(effect.category not in supported_effects for effect in effects):
                raise ValidationError("Unsupported Clan item effect")
            activation = item.get("activation")
            duration = item.get("duration_minutes")
            max_uses = item.get("max_uses_per_season")
            available = _boolean(item["available_s1"], "item availability")
            if available and activation not in {"TIMED", "CONTEXT"}:
                raise ValidationError("Available Clan item requires a supported activation")
            if activation == "TIMED":
                duration = _integer(duration, "item duration", minimum=1)
            elif duration is not None:
                raise ValidationError("Only timed items may define duration")
            if max_uses is not None:
                max_uses = _integer(max_uses, "item max uses", minimum=1)
            items.append(
                ClanItemConfig(
                    str(item["id"]),
                    str(item["name"]),
                    available,
                    _integer(item["price_treasury"], "item price", minimum=1),
                    activation,
                    duration,
                    effects,
                    max_uses,
                )
            )
        mission_bands = tuple(
            ClanMissionBand(
                str(key),
                _integer(value["participant_gross_points"], "mission gross", minimum=1),
                _integer(value["direct_hq_xp"], "mission hq xp"),
            )
            for key, value in mission_bands_raw.items()
        )
        result = ClanConfig(
            enabled=_boolean(raw["enabled"], "clan.enabled"),
            min_level=_integer(unlock["min_level"], "clan.unlock.min_level", minimum=1),
            base_capacity=_integer(base["base_capacity"], "clan base capacity", minimum=1),
            leave_cooldown_hours=_integer(
                leave["cooldown_hours"], "clan.leave.cooldown_hours", minimum=1
            ),
            max_voluntary_leaves_per_season=_integer(
                leave["max_voluntary_leaves_per_season"],
                "clan.leave.max_voluntary_leaves_per_season",
                minimum=1,
            ),
            access_rejoin_grace_hours=_integer(
                recovery["access_rejoin_grace_hours"],
                "clan.telegram_recovery.access_rejoin_grace_hours",
                minimum=1,
            ),
            soldier_warning_days=_integer(
                inactivity["soldier_warning_after_days"], "soldier warning", minimum=1
            ),
            soldier_removal_days=_integer(
                inactivity["soldier_removal_eligible_after_days"], "soldier removal", minimum=1
            ),
            captain_warning_days=_integer(
                inactivity["captain_warning_after_days"], "captain warning", minimum=1
            ),
            captain_recovery_days=_integer(
                inactivity["captain_recovery_after_days"], "captain recovery", minimum=1
            ),
            governance_vote_hours=_integer(
                governance["vote_duration_hours"], "governance duration", minimum=1
            ),
            governance_quorum_bps=_integer(
                governance["quorum_bps"], "governance quorum", minimum=1
            ),
            governance_approval_bps=_integer(
                governance["approval_threshold_bps"], "governance approval", minimum=1
            ),
            governance_minimum_voters=_integer(
                governance["minimum_eligible_voters"], "governance minimum voters", minimum=1
            ),
            bot_admin_restore_grace_hours=_integer(
                recovery["bot_admin_restore_grace_hours"], "bot admin restore grace", minimum=1
            ),
            hq_curve=hq_curve,
            max_capacity_including_items=_integer(
                capacity["max_capacity_including_items"], "capacity hard max", minimum=1
            ),
            activity=ClanActivityConfig(
                "CLAN_HOME",
                _integer(activity_raw["min_message_chars"], "clan min chars", minimum=1),
                _integer(
                    activity_raw["max_rewardable_chars_per_message"], "clan max chars", minimum=1
                ),
                _integer(activity_raw["max_messages_per_minute"], "clan rate", minimum=1),
                _integer(
                    activity_raw["qualified_chars_per_gross_point"],
                    "clan chars per point",
                    minimum=1,
                ),
                _integer(activity_raw["settlement_window_seconds"], "clan window", minimum=1),
                _integer(
                    activity_raw["max_gross_points_per_player_per_window"],
                    "clan window cap",
                    minimum=1,
                ),
            ),
            anti_fraud=ActivityFraudConfig(
                _integer(
                    fraud["exact_duplicate_window_seconds"], "clan duplicate window", minimum=1
                ),
                _integer(
                    fraud["near_duplicate_window_seconds"], "clan near duplicate window", minimum=1
                ),
                _integer(
                    fraud["same_fingerprint_suspicious_count"], "clan suspicious count", minimum=1
                ),
                _integer(fraud["same_fingerprint_reject_count"], "clan reject count", minimum=1),
            ),
            treasury_split_bps=_integer(treasury["split_bps"], "treasury split"),
            contribution=ClanContributionConfig(
                _integer(
                    contribution["gross_points_to_contribution_numerator"],
                    "contribution numerator",
                    minimum=1,
                ),
                _integer(
                    contribution["gross_points_to_contribution_denominator"],
                    "contribution denominator",
                    minimum=1,
                ),
                _integer(contribution["contribution_units_per_hq_xp"], "hq conversion", minimum=1),
                _integer(
                    contribution["contribution_units_to_clan_score_numerator"],
                    "score numerator",
                    minimum=1,
                ),
                _integer(
                    contribution["contribution_units_to_clan_score_denominator"],
                    "score denominator",
                    minimum=1,
                ),
            ),
            captain_bonus=CaptainBonusConfig(
                _boolean(bonus["enabled"], "captain bonus enabled"),
                _integer(bonus["share_bps"], "captain share"),
                _integer(bonus["max_points_per_day"], "captain cap"),
            ),
            mission_bands=mission_bands,
            inventory_stacking_rule=str(inventory["stacking_rule"]),
            inventory_items=tuple(items),
            united_front_threshold=_integer(
                achievements["united-front"]["min_season_contribution"], "united front", minimum=1
            ),
            treasury_keeper_threshold=_integer(
                achievements["treasury-keeper"]["min_lifetime_treasury_earned_in_season"],
                "treasury keeper",
                minimum=1,
            ),
        )
        if (
            result.soldier_warning_days >= result.soldier_removal_days
            or result.captain_warning_days >= result.captain_recovery_days
            or result.governance_quorum_bps > 10000
            or result.governance_approval_bps > 10000
            or result.treasury_split_bps > 10000
            or result.captain_bonus.share_bps > 10000
            or len({x.level for x in hq_curve}) != len(hq_curve)
            or len({x.min_xp for x in hq_curve}) != len(hq_curve)
            or any(
                b.level <= a.level or b.min_xp <= a.min_xp or b.base_capacity < a.base_capacity
                for a, b in zip(hq_curve, hq_curve[1:], strict=False)
            )
            or hq_curve[0].min_xp != 0
            or result.max_capacity_including_items < max(x.base_capacity for x in hq_curve)
            or len({x.item_id for x in items}) != len(items)
            or result.inventory_stacking_rule != "HIGHEST_BONUS_PER_EFFECT_CATEGORY"
            or activity_raw.get("context") != "CLAN_HOME"
            or activity_raw.get("daily_conversation_cap_enabled") is not False
            or treasury.get("rounding") != "FLOOR_ON_AGGREGATED_SETTLEMENT"
            or treasury.get("player_amount_is_gross_minus_treasury") is not True
            or treasury.get("spending_mints_player_points") is not False
            or contribution.get("calculated_before_treasury_split") is not True
            or bonus.get("base") != "OTHER_MEMBERS_ELIGIBLE_GROSS"
            or bonus.get("generates_contribution") is not False
            or bonus.get("eligible_for_referral_share") is not False
            or fraud.get("suspicious_is_auto_ban") is not False
        ):
            raise ValidationError("Clan thresholds and basis points are invalid")
        return result
    except (KeyError, TypeError) as exc:
        raise ValidationError("Invalid Clan configuration shape") from exc


class GameConfigUnavailable(LookupError):
    pass


def parse_progression(payload: dict[str, Any]) -> ProgressionConfig:
    try:
        raw = payload["progression"]
        raw_levels = raw["levels"]
        raw_stages = raw["dexo_stages"]
        if not isinstance(raw_levels, list) or not isinstance(raw_stages, list):
            raise TypeError
        levels = tuple(LevelRule(x["level"], x["min_lifetime_points"]) for x in raw_levels)
        stages = tuple(DexoStageRule(x["key"], x["min_level"]) for x in raw_stages)
    except (KeyError, TypeError) as exc:
        raise ValidationError("Invalid progression configuration shape") from exc
    if not levels or any(
        type(x.level) is not int or type(x.min_lifetime_points) is not int for x in levels
    ):
        raise ValidationError("Level values and thresholds must be integers")
    if levels[0] != LevelRule(1, 0):
        raise ValidationError("Level 1 must start at zero")
    if len({x.level for x in levels}) != len(levels) or any(
        x.min_lifetime_points < 0 for x in levels
    ):
        raise ValidationError("Levels must be unique with nonnegative thresholds")
    if list(levels) != sorted(levels, key=lambda x: (x.level, x.min_lifetime_points)):
        raise ValidationError("Levels must be deterministically ordered")
    if any(
        b.level <= a.level or b.min_lifetime_points <= a.min_lifetime_points
        for a, b in zip(levels, levels[1:], strict=False)
    ):
        raise ValidationError("Level thresholds must strictly ascend")
    if not stages or any(
        type(x.min_level) is not int or not isinstance(x.key, str) or not x.key for x in stages
    ):
        raise ValidationError("Dexo stages are invalid")
    if stages[0].min_level != 1 or len({x.key for x in stages}) != len(stages):
        raise ValidationError("Dexo stages require a unique level-one baseline")
    if any(b.min_level <= a.min_level for a, b in zip(stages, stages[1:], strict=False)):
        raise ValidationError("Dexo stage thresholds must strictly ascend")
    return ProgressionConfig(levels, stages)


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{name} must be a boolean")
    return value


def _timezone(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be an IANA timezone")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError(f"{name} must be an IANA timezone") from exc
    return value


def parse_daily(payload: dict[str, Any]) -> DailyConfig:
    try:
        raw = payload["daily"]
        rewards = tuple(_integer(x, "daily reward", minimum=1) for x in raw["reward_cycle_points"])
        result = DailyConfig(
            _boolean(raw["enabled"], "daily.enabled"),
            _timezone(raw["reset_timezone"], "daily.reset_timezone"),
            _integer(raw["reset_hour"], "daily.reset_hour"),
            _integer(raw["reset_minute"], "daily.reset_minute"),
            rewards,
            _boolean(raw["missed_full_period_resets_streak"], "daily streak reset"),
            _boolean(raw["catch_up_claims_enabled"], "daily catch-up"),
        )
        _boolean(raw["cycle_restarts_after_day_7"], "daily cycle restart")
        _boolean(raw["streak_counter_continues_after_day_7"], "daily streak continuation")
    except (KeyError, TypeError) as exc:
        raise ValidationError("Invalid daily configuration shape") from exc
    if not rewards or result.reset_hour > 23 or result.reset_minute > 59:
        raise ValidationError("Daily reset and reward cycle are invalid")
    return result


def parse_missions(payload: dict[str, Any]) -> MissionsConfig:
    supported = ("INTERNAL", "TELEGRAM", "TIMED_EXTERNAL")
    repeats = {"ONCE_PER_DEFINITION_VERSION", "DAILY"}
    try:
        raw = payload["missions"]
        families = tuple(raw["verification_families"])
        definitions = tuple(
            MissionRule(
                str(item["id"]),
                str(item["verification"]),
                _integer(item["reward_points"], "mission reward", minimum=1),
                str(item["repeat"]),
            )
            for item in raw["initial_definitions"]
        )
        bands = raw["reward_bands"]
        if not isinstance(bands, dict) or not bands:
            raise TypeError
        for name, amount in bands.items():
            if not isinstance(name, str):
                raise TypeError
            _integer(amount, "mission reward band", minimum=1)
        result = MissionsConfig(
            _boolean(raw["enabled"], "missions.enabled"),
            families,
            _integer(raw["timed_external_min_dwell_seconds"], "mission dwell", minimum=1),
            _integer(raw["economy_review_required_above_points"], "review threshold", minimum=1),
            definitions,
        )
    except (KeyError, TypeError) as exc:
        raise ValidationError("Invalid missions configuration shape") from exc
    if (
        families != supported
        or not definitions
        or len({x.key for x in definitions}) != len(definitions)
    ):
        raise ValidationError("Mission families or definition identities are invalid")
    if any(x.verification not in supported or x.repeat not in repeats for x in definitions):
        raise ValidationError("Unsupported mission verification or repeat policy")
    if any(x.reward_points > result.economy_review_required_above_points for x in definitions):
        raise ValidationError("Initial mission exceeds economy review threshold")
    return result


def parse_typing(payload: dict[str, Any]) -> TypingConfig:
    try:
        raw = payload["typing"]
        accuracy = tuple(
            TypingBracket(
                _integer(x["min_accuracy_bps"], "accuracy threshold"),
                _integer(x["points"], "accuracy points"),
            )
            for x in raw["accuracy_reward_brackets"]
        )
        wpm = tuple(
            TypingBracket(
                _integer(x["min_wpm"], "WPM threshold"), _integer(x["points"], "WPM points")
            )
            for x in raw["wpm_reward_brackets"]
        )
        anti = raw["anti_cheat"]
        for name in (
            "completed_invalid_attempt_consumes_quota",
            "technical_failure_consumes_quota",
            "claimable_wallet_enabled",
            "claim_anytime",
            "auto_settle_at_season_close",
            "claim_preserves_source_season",
        ):
            _boolean(raw[name], f"typing.{name}")
        for name in (
            "replayed_attempt_rejected",
            "impossible_timing_rejected",
            "suspicious_is_auto_ban",
        ):
            _boolean(anti[name], f"typing.anti_cheat.{name}")
        result = TypingConfig(
            _boolean(raw["enabled"], "typing.enabled"),
            _integer(raw["attempts_per_day"], "typing attempts", minimum=1),
            _timezone(raw["reset_timezone"], "typing.reset_timezone"),
            _integer(raw["challenge_seconds"], "challenge seconds", minimum=1),
            _boolean(raw["require_full_phrase_completion"], "full phrase completion"),
            _integer(raw["min_accuracy_bps"], "minimum accuracy"),
            _integer(raw["min_wpm"], "minimum WPM", minimum=1),
            accuracy,
            wpm,
            _integer(raw["max_reward_per_attempt"], "maximum reward"),
            _integer(anti["suspicious_wpm"], "suspicious WPM", minimum=1),
            _integer(anti["reject_wpm"], "reject WPM", minimum=1),
        )
    except (KeyError, TypeError) as exc:
        raise ValidationError("Invalid typing configuration shape") from exc
    if result.min_accuracy_bps > 10000 or not accuracy or not wpm:
        raise ValidationError("Typing thresholds are invalid")
    if any(b.threshold > 10000 for b in accuracy):
        raise ValidationError("Accuracy bracket exceeds 10000 bps")
    if any(b.threshold <= a.threshold for a, b in zip(accuracy, accuracy[1:], strict=False)) or any(
        b.threshold <= a.threshold for a, b in zip(wpm, wpm[1:], strict=False)
    ):
        raise ValidationError("Typing brackets must strictly ascend")
    if result.suspicious_wpm >= result.reject_wpm:
        raise ValidationError("Typing anti-cheat thresholds must ascend")
    if max(x.points for x in accuracy) + max(x.points for x in wpm) > result.max_reward_per_attempt:
        raise ValidationError("Typing bracket maximum exceeds configured maximum")
    return result


def parse_phase5(payload: dict[str, Any]) -> tuple[ReferralConfig, NetworkConfig, RetentionConfig]:
    try:
        referral = payload["referral"]
        activation = referral["activation"]
        bonus = referral["activation_bonus"]
        tiers = tuple(
            ReferralTier(
                str(item["key"]),
                _integer(item["min_verified_referrals"], "referral tier threshold", minimum=1),
                _integer(item["share_bps"], "referral share bps"),
            )
            for item in referral["tiers"]
        )
        referral_config = ReferralConfig(
            _boolean(referral["enabled"], "referral.enabled"),
            _integer(activation["min_level"], "referral activation level", minimum=1),
            _integer(activation["min_account_age_hours"], "referral account age"),
            _boolean(activation["anti_sybil_pass_required"], "referral anti sybil"),
            tuple(str(x) for x in activation["blocked_moderation_states"]),
            _integer(bonus["inviter_points"], "inviter bonus", minimum=1),
            _integer(bonus["invitee_points"], "invitee bonus", minimum=1),
            tiers,
            tuple(str(x) for x in referral["eligible_reward_sources"]),
        )
        network = payload["network_activity"]
        qualification = network["qualification"]
        creator = network["creator_reward"]
        speaker = network["speaker_reward"]
        fraud = payload["anti_fraud"]["network"]
        network_config = NetworkConfig(
            _boolean(network["enabled"], "network.enabled"),
            _integer(qualification["min_message_chars"], "minimum message chars", minimum=1),
            _integer(qualification["max_rewardable_chars_per_message"], "maximum chars", minimum=1),
            _integer(qualification["max_messages_per_minute"], "message rate", minimum=1),
            _boolean(speaker["enabled"], "speaker enabled"),
            _integer(speaker["points_per_unit"], "speaker points"),
            _integer(creator["aggregation_window_seconds"], "creator window", minimum=1),
            _integer(creator["qualified_chars_per_point"], "creator chars", minimum=1),
            _integer(creator["min_unique_speakers_per_window"], "unique speakers", minimum=1),
            _integer(creator["max_points_per_window_per_group"], "window cap", minimum=1),
            _integer(creator["max_points_per_creator_per_day"], "daily cap", minimum=1),
            _integer(creator["max_active_groups_per_creator"], "group cap", minimum=1),
            _integer(fraud["exact_duplicate_window_seconds"], "exact duplicate window", minimum=1),
            _integer(
                fraud["near_duplicate_window_seconds"], "normalized duplicate window", minimum=1
            ),
            _integer(fraud["same_fingerprint_suspicious_count"], "suspicious count", minimum=1),
            _integer(fraud["same_fingerprint_reject_count"], "reject count", minimum=1),
        )
        retention = payload["retention"]
        retention_config = RetentionConfig(
            _integer(retention["message_content_days"], "content retention", minimum=1),
            _integer(
                retention["message_fingerprint_metadata_days"], "fingerprint retention", minimum=1
            ),
            _integer(
                retention["moderation_case_evidence_after_close_days"],
                "moderation evidence after close retention",
                minimum=1,
            ),
        )
    except (KeyError, TypeError) as exc:
        raise ValidationError("Invalid Phase 5 configuration shape") from exc
    if not tiers or any(t.share_bps > 10000 for t in tiers):
        raise ValidationError("Referral tiers are invalid")
    if any(
        b.min_verified_referrals <= a.min_verified_referrals
        for a, b in zip(tiers, tiers[1:], strict=False)
    ):
        raise ValidationError("Referral tier thresholds must strictly ascend")
    if network_config.min_message_chars > network_config.max_rewardable_chars:
        raise ValidationError("Network message character bounds are invalid")
    if network_config.suspicious_count >= network_config.reject_count:
        raise ValidationError("Network duplicate thresholds must ascend")
    eligible = referral_config.eligible_reward_sources
    ineligible = tuple(str(x) for x in referral["ineligible_reward_sources"])
    if (
        referral.get("first_touch_immutable") is not True
        or referral.get("self_referral_allowed") is not False
        or referral.get("share_base") != "PLAYER_AMOUNT"
        or referral.get("share_is_deducted_from_invitee") is not False
        or referral.get("recursive_referral_rewards_forbidden") is not True
        or len(set(eligible)) != len(eligible)
        or len(set(ineligible)) != len(ineligible)
        or set(eligible) & set(ineligible)
        or {"REFERRAL", "CREATOR"} & set(eligible)
        or creator.get("creator_self_activity_eligible") is not False
        or fraud.get("suspicious_is_auto_ban") is not False
    ):
        raise ValidationError("Phase 5 safety invariants are invalid")
    return referral_config, network_config, retention_config


def parse_economy_sync(payload: dict[str, Any]) -> tuple[BaseTapConfig, DlxDistributionConfig]:
    try:
        meta = payload["meta"]
        if meta["schema_version"] != 2 or meta["economy_contract_version"] != "2.0.0":
            raise TypeError
        tap = payload["base_tap"]
        base_tap = BaseTapConfig(
            _boolean(tap["enabled"], "base_tap.enabled"),
            _integer(tap["points_per_rewarded_tap"], "base tap reward", minimum=1),
            _integer(tap["rewarded_taps_per_utc_day"], "base tap daily cap", minimum=1),
            _timezone(tap["reset_timezone"], "base_tap.reset_timezone"),
            _integer(tap["max_taps_per_batch"], "base tap batch cap", minimum=1),
            _integer(tap["min_batch_interval_ms"], "base tap interval", minimum=0),
            _integer(tap["receipt_retention_days"], "base tap receipt retention", minimum=1),
        )
        distribution = payload["dlx_distribution"]
        timing = distribution["timing"]
        pool = distribution["pool"]
        weights = distribution["category_weights_bps"]
        if not isinstance(weights, dict):
            raise TypeError
        parsed_weights = tuple(
            (str(key), _integer(value, "DLX category weight")) for key, value in weights.items()
        )
        growth = pool["growth_unlock"]
        predicate = growth["qualified_player_predicate"]
        if not isinstance(predicate, dict) or not predicate:
            raise TypeError
        milestones = tuple(
            GrowthMilestone(
                str(item["key"]),
                _integer(item["qualified_player_count"], "qualified player milestone", minimum=1),
                _integer(item["unlock_dlx_amount"], "milestone unlock", minimum=1),
            )
            for item in growth["milestones"]
        )
        result = DlxDistributionConfig(
            _integer(timing["product_period_days"], "product period", minimum=1),
            _integer(timing["epoch_count"], "epoch count", minimum=1),
            _integer(timing["epoch_days"], "epoch days", minimum=1),
            _integer(timing["closing_days"], "closing days", minimum=0),
            _integer(distribution["default_settlement_start_epoch"], "settlement start", minimum=1),
            _integer(pool["base_dlx_amount"], "base DLX pool"),
            _integer(pool["max_dlx_amount"], "maximum DLX pool"),
            parsed_weights,
            predicate,
            milestones,
        )
    except (KeyError, TypeError) as exc:
        raise ValidationError("Invalid economy sync configuration shape") from exc
    if base_tap.reset_timezone != "UTC":
        raise ValidationError("Base Tap resets at 00:00 UTC")
    if base_tap.max_taps_per_batch > base_tap.rewarded_taps_per_day:
        raise ValidationError("Base Tap batch cap cannot exceed its daily cap")
    if result.epoch_count != 6 or result.epoch_days != 7:
        raise ValidationError("Season 1 requires exactly six seven-day economic epochs")
    if result.product_period_days != result.epoch_count * result.epoch_days + result.closing_days:
        raise ValidationError("Product period must equal economic epochs plus closing days")
    if result.product_period_days != 45 or result.closing_days != 3:
        raise ValidationError("Season 1 requires a 45-day period and three closing days")
    if result.default_settlement_start_epoch > result.epoch_count:
        raise ValidationError("Settlement start epoch is outside the economic epoch range")
    if result.max_pool_dlx_amount < result.base_pool_dlx_amount:
        raise ValidationError("Maximum DLX pool cannot be below the base pool")
    expected_categories = {
        "CLAN_PERFORMANCE",
        "CORE_GAMEPLAY",
        "CREATOR_COMMUNITY_GROWTH",
        "VERIFIED_REFERRAL",
        "LOYALTY_CONSISTENCY",
    }
    if {key for key, _ in parsed_weights} != expected_categories or sum(
        value for _, value in parsed_weights
    ) != 10000:
        raise ValidationError(
            "DLX category weights must contain the canonical categories and total 10000 bps"
        )
    if len({item.key for item in milestones}) != len(milestones) or len(
        {item.qualified_player_count for item in milestones}
    ) != len(milestones):
        raise ValidationError("Growth milestones require unique keys and thresholds")
    available_growth = result.max_pool_dlx_amount - result.base_pool_dlx_amount
    if sum(item.unlock_dlx_amount for item in milestones) > available_growth:
        raise ValidationError("Growth milestone unlocks exceed the maximum DLX pool")
    return base_tap, result


CANONICAL_ACHIEVEMENTS = (
    "first-mission",
    "first-referral",
    "seven-day-streak",
    "thirty-day-streak",
    "first-verified-activity",
    "clan-ready",
    "first-clan",
    "first-contribution",
    "first-war",
    "first-victory",
)
CANONICAL_ACHIEVEMENT_TARGETS = {
    "first-mission": 1,
    "first-referral": 1,
    "seven-day-streak": 7,
    "thirty-day-streak": 30,
    "first-verified-activity": 1,
    "first-clan": 1,
    "first-contribution": 1,
}
CANONICAL_MEDALS = (
    "season-founding",
    "season-participant",
    "season-veteran",
    "season-top-10",
    "season-top-1",
    "season-champion",
    "activity-veteran",
    "clan-loyalty",
    "leadership",
    "mission-master",
)
CANONICAL_TITLES = {
    "explorer": "DEFAULT",
    "early-pioneer": "MEDAL:season-founding",
    "mission-runner": "ACHIEVEMENT:first-mission",
    "network-builder": "ACHIEVEMENT:first-referral",
    "community-voice": "ACHIEVEMENT:first-verified-activity",
    "season-veteran": "MEDAL:season-veteran",
}
CANONICAL_FRAMES = {
    "standard": "DEFAULT",
    "premium": "LEVEL",
    "seasonal": "MEDAL:season-participant",
    "founder": "MEDAL:season-founding",
    "clan": "ACTIVE_CLAN_MEMBERSHIP",
    "captain": "ACTIVE_CLAN_CAPTAIN",
}


def _config_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    return value


def parse_profile_collection(
    payload: dict[str, Any],
) -> tuple[ProfileCollectionConfig, SeasonHonorsConfig]:
    """Parse only the explicit Season 1 collection rule shapes."""
    try:
        raw = _config_object(payload["profile_collection"], "profile_collection")
        honors = _config_object(payload["season_honors"], "season_honors")
        raw_achievements = _config_object(raw["achievements"], "achievements")
        raw_titles = _config_object(raw["titles"], "titles")
        raw_frames = _config_object(raw["frames"], "frames")
        raw_medals = _config_object(honors["medals"], "medals")
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValidationError("Invalid profile collection configuration shape") from exc
    if raw.get("achievement_point_rewards_enabled") is not False:
        raise ValidationError("Achievement point rewards must remain disabled")
    if honors.get("point_rewards_enabled") is not False:
        raise ValidationError("Medal point rewards must remain disabled")
    if set(raw_achievements) != set(CANONICAL_ACHIEVEMENTS):
        raise ValidationError("Profile configuration requires the canonical achievement keys")
    achievements: list[AchievementRule] = []
    for key in CANONICAL_ACHIEVEMENTS:
        rule = _config_object(raw_achievements[key], f"achievement {key}")
        if key in {"first-war", "first-victory"}:
            if rule.get("available_s1") is not False:
                raise ValidationError("War achievements must remain unavailable in Season 1")
            achievements.append(AchievementRule(key, False, 0))
            continue
        if rule.get("point_reward") != 0:
            raise ValidationError("Every Season 1 achievement point reward must be zero")
        target_name = (
            "min_level"
            if key == "clan-ready"
            else ("min_contribution" if key == "first-contribution" else "target")
        )
        target = _integer(rule.get(target_name), f"{key}.{target_name}", minimum=1)
        if (
            key
            in {
                "first-mission",
                "first-referral",
                "seven-day-streak",
                "thirty-day-streak",
                "first-verified-activity",
                "first-clan",
                "first-contribution",
            }
            and target != CANONICAL_ACHIEVEMENT_TARGETS[key]
        ):
            raise ValidationError(f"{key} must use the canonical Season 1 target")
        achievements.append(AchievementRule(key, True, target))
    clan_ready = next(item for item in achievements if item.key == "clan-ready")
    try:
        clan = _config_object(payload["clan"], "clan")
        clan_unlock = _config_object(clan["unlock"], "clan.unlock")
        clan_min_level = _integer(clan_unlock["min_level"], "clan.unlock.min_level", minimum=1)
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValidationError("Invalid Clan unlock configuration shape") from exc
    if clan_ready.target != clan_min_level:
        raise ValidationError("Clan Ready and Clan unlock level requirements must match")
    if set(raw_medals) != set(CANONICAL_MEDALS):
        raise ValidationError("Season honors require the canonical medal keys")
    medal_fields = {
        "season-participant": "min_season_points",
        "season-veteran": "min_season_points",
        "season-top-10": "max_rank_percentile_bps",
        "season-top-1": "max_rank_percentile_bps",
        "season-champion": "max_rank",
        "activity-veteran": "min_verified_activity_events",
        "clan-loyalty": "min_active_days_same_clan",
        "mission-master": "min_claimed_missions",
    }
    medals: list[MedalRule] = []
    for key in CANONICAL_MEDALS:
        rule = _config_object(raw_medals[key], f"medal {key}")
        field = medal_fields.get(key)
        if field and field not in rule:
            raise ValidationError(f"medal {key} is missing {field}")
        threshold = _integer(rule[field], f"{key}.{field}", minimum=1) if field else None
        medals.append(MedalRule(key, threshold))
    thresholds = {item.key: item.threshold for item in medals}
    top_ten = thresholds["season-top-10"]
    top_one = thresholds["season-top-1"]
    participant = thresholds["season-participant"]
    veteran = thresholds["season-veteran"]
    assert top_ten is not None and top_one is not None
    assert participant is not None and veteran is not None
    if top_ten > 10000 or top_one > 10000 or top_one > top_ten:
        raise ValidationError("Season medal percentiles must be ordered within 1..10000 bps")
    if participant > veteran:
        raise ValidationError("Season Participant threshold cannot exceed Season Veteran")

    def collection(
        catalog: dict[str, Any], expected: dict[str, str], kind: str
    ) -> tuple[CollectionRule, ...]:
        if set(catalog) != set(expected):
            raise ValidationError(f"Profile configuration requires canonical {kind} keys")
        rows = []
        for key, expected_unlock in expected.items():
            rule = _config_object(catalog[key], f"{kind} {key}")
            unlock = rule.get("unlock")
            if expected_unlock == "LEVEL":
                if not isinstance(unlock, str) or not unlock.startswith("LEVEL:"):
                    raise ValidationError(f"{kind} {key} must use a Level entitlement")
                try:
                    if int(unlock.removeprefix("LEVEL:")) < 1:
                        raise ValueError
                except ValueError as exc:
                    raise ValidationError(f"{kind} {key} has an invalid Level entitlement") from exc
            elif unlock != expected_unlock:
                raise ValidationError(f"{kind} {key} must use its canonical entitlement")
            rows.append(CollectionRule(key, unlock))
        return tuple(rows)

    titles = collection(raw_titles, CANONICAL_TITLES, "title")
    frames = collection(raw_frames, CANONICAL_FRAMES, "frame")
    return ProfileCollectionConfig(tuple(achievements), titles, frames), SeasonHonorsConfig(
        tuple(medals)
    )


def parse_game_config(
    payload: dict[str, Any],
) -> tuple[ProgressionConfig, DailyConfig, MissionsConfig, TypingConfig]:
    return (
        parse_progression(payload),
        parse_daily(payload),
        parse_missions(payload),
        parse_typing(payload),
    )


def _parse_resolved(
    payload: dict[str, Any],
) -> tuple[
    ProgressionConfig,
    DailyConfig,
    MissionsConfig,
    TypingConfig,
    ReferralConfig,
    NetworkConfig,
    RetentionConfig,
    BaseTapConfig,
    DlxDistributionConfig | None,
]:
    """Resolve pre-Phase-4 snapshots without rewriting their immutable payloads.

    Feature services fail closed because each compatibility policy is disabled.
    """
    progression = parse_progression(payload)
    if not any(name in payload for name in ("daily", "missions", "typing")):
        return (
            progression,
            DailyConfig(False, "UTC", 0, 0, (), True, False),
            MissionsConfig(False, ("INTERNAL", "TELEGRAM", "TIMED_EXTERNAL"), 1, 1, ()),
            TypingConfig(False, 1, "UTC", 1, True, 0, 1, (), (), 0, 1, 2),
            ReferralConfig(False, 1, 0, True, (), 1, 1, (), ()),
            NetworkConfig(False, 1, 1, 1, False, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2),
            RetentionConfig(1, 1, 1),
            BaseTapConfig(False, 1, 1, "UTC", 1, 0, 1),
            None,
        )
    if "referral" not in payload:
        progression, daily, missions, typing = (
            parse_progression(payload),
            parse_daily(payload),
            parse_missions(payload),
            parse_typing(payload),
        )
        return (
            progression,
            daily,
            missions,
            typing,
            ReferralConfig(False, 1, 0, True, (), 1, 1, (), ()),
            NetworkConfig(False, 1, 1, 1, False, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2),
            RetentionConfig(1, 1, 1),
            BaseTapConfig(False, 1, 1, "UTC", 1, 0, 1),
            None,
        )
    progression, daily, missions, typing = parse_game_config(payload)
    referral, network, retention = parse_phase5(payload)
    if "base_tap" not in payload:
        return (
            progression,
            daily,
            missions,
            typing,
            referral,
            network,
            retention,
            BaseTapConfig(False, 1, 1, "UTC", 1, 0, 1),
            None,
        )
    base_tap, distribution = parse_economy_sync(payload)
    return (
        progression,
        daily,
        missions,
        typing,
        referral,
        network,
        retention,
        base_tap,
        distribution,
    )


def publish_game_config(
    config: GameConfigVersion, *, published_at: datetime | None = None
) -> GameConfigVersion:
    with transaction.atomic():
        config = GameConfigVersion.objects.select_for_update().get(pk=config.pk)
        if config.status != GameConfigVersion.Status.DRAFT:
            raise ValidationError("Only draft configurations can be published")
        if "referral" in config.payload:
            parse_game_config(config.payload)
            parse_phase5(config.payload)
            if "base_tap" in config.payload:
                parse_economy_sync(config.payload)
                if config.schema_version != config.payload["meta"]["schema_version"]:
                    raise ValidationError("GameConfig schema version does not match its payload")
            if "profile_collection" in config.payload:
                parse_profile_collection(config.payload)
            if "clan" in config.payload:
                parse_clan(config.payload)
        elif any(name in config.payload for name in ("daily", "missions", "typing")):
            parse_progression(config.payload)
            parse_daily(config.payload)
            parse_missions(config.payload)
            parse_typing(config.payload)
        else:
            parse_progression(config.payload)
        publication_time = published_at or timezone.now()
        published_configs = GameConfigVersion.objects.select_for_update().filter(
            status=GameConfigVersion.Status.PUBLISHED
        )
        timeline_exists = published_configs.exists()
        if timeline_exists and config.effective_at < publication_time:
            raise ValidationError(
                "A subsequent configuration cannot become effective before publication"
            )
        if published_configs.filter(effective_at__gte=config.effective_at).exists():
            raise ValidationError(
                "Publishing would rewrite or ambiguate established config history"
            )
        canonical = json.dumps(config.payload, sort_keys=True, separators=(",", ":")).encode()
        config.payload_checksum = hashlib.sha256(canonical).hexdigest()
        config.status = GameConfigVersion.Status.PUBLISHED
        config.published_at = publication_time
        config.save(update_fields=["payload_checksum", "status", "published_at", "updated_at"])
    logger.info("game_config_published", config_version=config.version)
    return config


def resolve_effective_game_config(at: datetime) -> ResolvedGameConfig:
    configs = list(
        GameConfigVersion.objects.filter(
            status=GameConfigVersion.Status.PUBLISHED, effective_at__lte=at
        ).order_by("-effective_at", "-pk")[:2]
    )
    if not configs:
        raise GameConfigUnavailable("No published game configuration is effective")
    config = configs[0]
    progression, daily, missions, typing, referral, network, retention, base_tap, distribution = (
        _parse_resolved(config.payload)
    )
    profile, honors = (
        parse_profile_collection(config.payload)
        if "profile_collection" in config.payload
        else (None, None)
    )
    return ResolvedGameConfig(
        config.pk,
        config.version,
        config.effective_at,
        progression,
        daily,
        missions,
        typing,
        referral,
        network,
        retention,
        base_tap,
        distribution,
        profile,
        honors,
        parse_clan(config.payload) if "clan" in config.payload else None,
    )


def resolve_game_config_version(config_id: int) -> ResolvedGameConfig:
    try:
        config = GameConfigVersion.objects.get(
            pk=config_id, status=GameConfigVersion.Status.PUBLISHED
        )
    except GameConfigVersion.DoesNotExist as exc:
        raise GameConfigUnavailable("Published game configuration is unavailable") from exc
    progression, daily, missions, typing, referral, network, retention, base_tap, distribution = (
        _parse_resolved(config.payload)
    )
    profile, honors = (
        parse_profile_collection(config.payload)
        if "profile_collection" in config.payload
        else (None, None)
    )
    return ResolvedGameConfig(
        config.pk,
        config.version,
        config.effective_at,
        progression,
        daily,
        missions,
        typing,
        referral,
        network,
        retention,
        base_tap,
        distribution,
        profile,
        honors,
        parse_clan(config.payload) if "clan" in config.payload else None,
    )
