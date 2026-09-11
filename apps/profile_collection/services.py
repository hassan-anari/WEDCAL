from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from apps.daily.models import DailyClaim
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import CollectionRule, ResolvedGameConfig
from apps.missions.models import PlayerMission
from apps.network.models import NetworkActivity
from apps.players.models import Player
from apps.profile_collection.models import (
    PlayerAchievementUnlock,
    PlayerMedalAward,
    PlayerProfile,
)
from apps.progression.services import refresh_player_progression
from apps.referrals.models import ReferralRelation


class ClanEntitlementProvider(Protocol):
    def active_membership(self, player: Player) -> bool: ...
    def ever_member(self, player: Player) -> bool: ...
    def active_captain(self, player: Player) -> bool: ...
    def contribution_units(self, player: Player) -> int: ...


class NoClanEntitlements:
    """Fail-closed Phase 6 seam; Phase 7/8 can supply canonical Clan facts."""

    def active_membership(self, player: Player) -> bool:
        return False

    def active_captain(self, player: Player) -> bool:
        return False

    def ever_member(self, player: Player) -> bool:
        return False

    def contribution_units(self, player: Player) -> int:
        return 0


class ClanEntitlements:
    def active_membership(self, player: Player) -> bool:
        from apps.clans.models import ClanMembership

        return ClanMembership.objects.filter(player=player, ended_at__isnull=True).exists()

    def active_captain(self, player: Player) -> bool:
        from apps.clans.models import ClanMembership, ClanRole

        return ClanMembership.objects.filter(
            player=player, ended_at__isnull=True, role=ClanRole.CAPTAIN
        ).exists()

    def ever_member(self, player: Player) -> bool:
        from apps.clans.models import ClanMembership

        return ClanMembership.objects.filter(player=player).exists()

    def contribution_units(self, player: Player) -> int:
        return 0


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    claimed_missions: int
    verified_referrals: int
    current_daily_streak: int
    maximum_daily_streak: int
    valid_verified_activity: int
    level: int
    active_clan: bool
    ever_clan: bool
    active_captain: bool
    contribution_units: int


def get_or_create_profile(player: Player) -> PlayerProfile:
    profile, _ = PlayerProfile.objects.get_or_create(player=player)
    return profile


def collect_profile_facts(
    player: Player,
    resolved: ResolvedGameConfig,
    clan: ClanEntitlementProvider | None = None,
) -> ProfileFacts:
    clan = clan or ClanEntitlements()
    progression, _ = refresh_player_progression(player, resolved)
    claims = DailyClaim.objects.filter(player=player)
    latest_claim = (
        claims.order_by("-period_start").values_list("streak_count_after_claim", flat=True).first()
    )
    maximum = claims.aggregate(value=Max("streak_count_after_claim"))["value"] or 0
    return ProfileFacts(
        claimed_missions=PlayerMission.objects.filter(
            player=player, status=PlayerMission.Status.CLAIMED
        ).count(),
        verified_referrals=ReferralRelation.objects.filter(
            inviter=player, status=ReferralRelation.Status.VERIFIED
        ).count(),
        current_daily_streak=latest_claim or 0,
        maximum_daily_streak=maximum,
        valid_verified_activity=NetworkActivity.objects.filter(
            sender=player,
            classification=NetworkActivity.Classification.VALID,
            program_active=True,
        ).count(),
        level=progression.level,
        active_clan=clan.active_membership(player),
        ever_clan=clan.ever_member(player),
        active_captain=clan.active_captain(player),
        contribution_units=clan.contribution_units(player),
    )


def achievement_progress(key: str, facts: ProfileFacts) -> int:
    return {
        "first-mission": facts.claimed_missions,
        "first-referral": facts.verified_referrals,
        "seven-day-streak": facts.maximum_daily_streak,
        "thirty-day-streak": facts.maximum_daily_streak,
        "first-verified-activity": facts.valid_verified_activity,
        "clan-ready": facts.level,
        "first-clan": int(facts.ever_clan),
        "first-contribution": facts.contribution_units,
        "first-war": 0,
        "first-victory": 0,
    }[key]


@transaction.atomic
def reconcile_profile(
    player: Player, resolved: ResolvedGameConfig, at: datetime
) -> tuple[
    PlayerProfile, ProfileFacts, dict[str, PlayerAchievementUnlock], dict[str, PlayerMedalAward]
]:
    if resolved.profile_collection is None or resolved.season_honors is None:
        raise ValidationError("Profile collection is unavailable in the effective configuration")
    player = Player.objects.select_for_update().get(pk=player.pk)
    profile = get_or_create_profile(player)
    profile = PlayerProfile.objects.select_for_update().get(pk=profile.pk)
    facts = collect_profile_facts(player, resolved)
    config = GameConfigVersion.objects.get(pk=resolved.id)
    existing = {x.achievement_key: x for x in PlayerAchievementUnlock.objects.filter(player=player)}
    for rule in resolved.profile_collection.achievements:
        if rule.available_s1 and achievement_progress(rule.key, facts) >= rule.target:
            unlock, _ = PlayerAchievementUnlock.objects.get_or_create(
                player=player,
                achievement_key=rule.key,
                defaults={
                    "unlocked_at": at,
                    "config_version": config,
                    "evidence_category": rule.key.upper().replace("-", "_"),
                },
            )
            existing[rule.key] = unlock
    awards = {x.medal_key: x for x in PlayerMedalAward.objects.filter(player=player)}
    medal_thresholds = {x.key: x.threshold for x in resolved.season_honors.medals}
    eligible = {
        "activity-veteran": facts.valid_verified_activity,
        "mission-master": facts.claimed_missions,
    }
    for key, progress in eligible.items():
        threshold = medal_thresholds[key]
        assert threshold is not None
        if progress >= threshold:
            award, _ = PlayerMedalAward.objects.get_or_create(
                player=player,
                medal_key=key,
                source_season=None,
                defaults={
                    "awarded_at": at,
                    "config_version": config,
                    "reason": key.upper().replace("-", "_"),
                },
            )
            awards[key] = award
    achievement_keys, medal_keys = set(existing), set(awards)
    frames = {x.key: x for x in resolved.profile_collection.frames}
    titles = {x.key: x for x in resolved.profile_collection.titles}
    if profile.active_frame_key not in frames or not entitlement(
        frames[profile.active_frame_key], facts, achievement_keys, medal_keys
    ):
        PlayerProfile.objects.filter(
            pk=profile.pk, active_frame_key=profile.active_frame_key
        ).update(active_frame_key="standard")
    if profile.active_title_key not in titles or not entitlement(
        titles[profile.active_title_key], facts, achievement_keys, medal_keys
    ):
        PlayerProfile.objects.filter(
            pk=profile.pk, active_title_key=profile.active_title_key
        ).update(active_title_key="explorer")
    if profile.featured_medal_key is not None and profile.featured_medal_key not in medal_keys:
        PlayerProfile.objects.filter(
            pk=profile.pk, featured_medal_key=profile.featured_medal_key
        ).update(featured_medal_key=None)
    if (
        profile.featured_achievement_key is not None
        and profile.featured_achievement_key not in achievement_keys
    ):
        PlayerProfile.objects.filter(
            pk=profile.pk, featured_achievement_key=profile.featured_achievement_key
        ).update(featured_achievement_key=None)
    profile.refresh_from_db()
    return profile, facts, existing, awards


def entitlement(
    rule: CollectionRule, facts: ProfileFacts, achievements: set[str], medals: set[str]
) -> bool:
    unlock = rule.unlock
    if unlock == "DEFAULT":
        return True
    if unlock.startswith("LEVEL:"):
        return facts.level >= int(unlock.removeprefix("LEVEL:"))
    if unlock.startswith("ACHIEVEMENT:"):
        return unlock.removeprefix("ACHIEVEMENT:") in achievements
    if unlock.startswith("MEDAL:"):
        return unlock.removeprefix("MEDAL:") in medals
    if unlock == "ACTIVE_CLAN_MEMBERSHIP":
        return facts.active_clan
    if unlock == "ACTIVE_CLAN_CAPTAIN":
        return facts.active_captain
    return False


@transaction.atomic
def review_profile(player: Player, at: datetime) -> PlayerProfile:
    player = Player.objects.select_for_update().get(pk=player.pk)
    profile = get_or_create_profile(player)
    profile = PlayerProfile.objects.select_for_update().get(pk=profile.pk)
    if profile.first_reviewed_at is None:
        profile.first_reviewed_at = at
        profile.save(update_fields=("first_reviewed_at", "updated_at"))
    return profile


@transaction.atomic
def update_customization(
    player: Player,
    resolved: ResolvedGameConfig,
    at: datetime,
    *,
    active_frame_key: str,
    active_title_key: str,
    featured_medal_key: str | None,
    featured_achievement_key: str | None,
) -> PlayerProfile:
    profile, facts, achievements, awards = reconcile_profile(player, resolved, at)
    assert resolved.profile_collection is not None
    frames = {x.key: x for x in resolved.profile_collection.frames}
    titles = {x.key: x for x in resolved.profile_collection.titles}
    achievement_keys, medal_keys = set(achievements), set(awards)
    if active_frame_key not in frames or not entitlement(
        frames[active_frame_key], facts, achievement_keys, medal_keys
    ):
        raise ValidationError("Frame is not available to this player")
    if active_title_key not in titles or not entitlement(
        titles[active_title_key], facts, achievement_keys, medal_keys
    ):
        raise ValidationError("Title is not available to this player")
    if featured_medal_key is not None and featured_medal_key not in medal_keys:
        raise ValidationError("Medal is not owned by this player")
    if featured_achievement_key is not None and featured_achievement_key not in achievement_keys:
        raise ValidationError("Achievement is not unlocked by this player")
    profile = PlayerProfile.objects.select_for_update().get(pk=profile.pk)
    profile.active_frame_key = active_frame_key
    profile.active_title_key = active_title_key
    profile.featured_medal_key = featured_medal_key
    profile.featured_achievement_key = featured_achievement_key
    profile.save()
    return profile
