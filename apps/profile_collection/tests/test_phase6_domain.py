# mypy: disable-error-code="no-untyped-def"
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.daily.models import DailyClaim
from apps.game_config.services import resolve_effective_game_config
from apps.missions.models import MissionDefinition, PlayerMission
from apps.missions.services import claim_mission, start_mission, verify_mission
from apps.network.models import CreatorGroupRegistration, NetworkActivity
from apps.profile_collection.models import PlayerAchievementUnlock, PlayerMedalAward
from apps.profile_collection.services import reconcile_profile, review_profile, update_customization
from apps.progression.models import PlayerProgression
from apps.referrals.models import ReferralRelation
from apps.rewards.models import RewardLedger
from apps.telegram.models import TelegramGroup

BASE = datetime(2026, 1, 2, tzinfo=UTC)


def mission(
    key: str, *, status: str | None = None, player=None
) -> PlayerMission | MissionDefinition:
    definition = MissionDefinition.objects.create(
        key=key,
        version=1,
        enabled=True,
        verification_family=MissionDefinition.Verification.INTERNAL,
        repeat_policy=MissionDefinition.Repeat.ONCE,
    )
    if player is None:
        return definition
    return PlayerMission.objects.create(
        player=player,
        definition=definition,
        eligibility_period="definition",
        status=status or PlayerMission.Status.IN_PROGRESS,
        claimed_at=BASE if status == PlayerMission.Status.CLAIMED else None,
    )


def network_registration(player, at: datetime) -> CreatorGroupRegistration:
    group = TelegramGroup.objects.create(
        telegram_chat_id=-(player.telegram_user_id + 1000),
        chat_type=TelegramGroup.ChatType.SUPERGROUP,
        first_seen_at=at,
        last_seen_at=at,
    )
    return CreatorGroupRegistration.objects.create(creator=player, group=group)


def activity(registration, player, config, classification: str, message_id: int, *, active=True):
    at = BASE + timedelta(minutes=message_id)
    return NetworkActivity.objects.create(
        registration=registration,
        group=registration.group,
        sender=player,
        telegram_message_id=message_id,
        telegram_update_id=message_id,
        observed_at=at,
        classification=classification,
        program_active=active,
        config_version=config,
        content_expires_at=at + timedelta(days=1),
        fingerprint_expires_at=at + timedelta(days=1),
    )


@pytest.mark.django_db
def test_durable_facts_unlock_achievements_and_only_current_career_medals(
    phase4_config_factory, player_factory, season_factory
) -> None:
    def tune(data):
        data["profile_collection"]["achievements"]["clan-ready"]["min_level"] = 2
        data["clan"]["unlock"]["min_level"] = 2
        data["season_honors"]["medals"]["activity-veteran"]["min_verified_activity_events"] = 1
        data["season_honors"]["medals"]["mission-master"]["min_claimed_missions"] = 1

    config = phase4_config_factory(mutate=tune)
    season = season_factory(config)
    player = player_factory(610)
    invitee = player_factory(611)
    claimed = mission("claimed-fact", status=PlayerMission.Status.CLAIMED, player=player)
    assert isinstance(claimed, PlayerMission)
    ReferralRelation.objects.create(
        inviter=player,
        invitee=invitee,
        status=ReferralRelation.Status.VERIFIED,
        attributed_at=BASE,
        verified_at=BASE,
        verified_config=config,
    )
    DailyClaim.objects.create(
        player=player,
        season=season,
        claim_period="historic-30",
        period_start=BASE,
        period_end=BASE + timedelta(days=1),
        streak_count_after_claim=30,
        cycle_day=2,
        reward_amount=1,
        config_version=config,
        claimed_at=BASE,
    )
    PlayerProgression.objects.create(
        player=player, lifetime_points=300, level=1, dexo_stage="newborn", config_version=config
    )
    registration = network_registration(player, BASE)
    activity(registration, player, config, NetworkActivity.Classification.VALID, 1)

    resolved = resolve_effective_game_config(BASE)
    _, facts, achievements, medals = reconcile_profile(player, resolved, BASE)
    assert facts.claimed_missions == facts.verified_referrals == facts.valid_verified_activity == 1
    assert facts.maximum_daily_streak == 30
    assert {
        "first-mission",
        "first-referral",
        "seven-day-streak",
        "thirty-day-streak",
        "first-verified-activity",
        "clan-ready",
    } <= set(achievements)
    assert {"first-clan", "first-contribution", "first-war", "first-victory"}.isdisjoint(
        achievements
    )
    assert set(medals) == {"activity-veteran", "mission-master"}
    assert RewardLedger.objects.filter(player=player).count() == 0

    DailyClaim.objects.create(
        player=player,
        season=season,
        claim_period="current-reset",
        period_start=BASE + timedelta(days=3),
        period_end=BASE + timedelta(days=4),
        streak_count_after_claim=1,
        cycle_day=1,
        reward_amount=1,
        config_version=config,
        claimed_at=BASE + timedelta(days=3),
    )
    _, later_facts, later_achievements, later_medals = reconcile_profile(
        player, resolved, BASE + timedelta(days=3)
    )
    assert later_facts.current_daily_streak == 1
    assert {"seven-day-streak", "thirty-day-streak"} <= set(later_achievements)
    assert set(later_medals) == set(medals)
    assert PlayerAchievementUnlock.objects.filter(player=player).count() == len(later_achievements)
    assert PlayerMedalAward.objects.filter(player=player).count() == 2


@pytest.mark.django_db
def test_invalid_or_inactive_network_activity_never_unlocks_or_awards(
    phase4_config_factory, player_factory
) -> None:
    def tune(data):
        data["season_honors"]["medals"]["activity-veteran"]["min_verified_activity_events"] = 1

    config = phase4_config_factory(mutate=tune)
    player = player_factory(620)
    registration = network_registration(player, BASE)
    for message_id, classification in enumerate(
        [
            NetworkActivity.Classification.SUSPICIOUS,
            NetworkActivity.Classification.REJECTED,
            NetworkActivity.Classification.CONTROL,
            NetworkActivity.Classification.LATE,
        ],
        1,
    ):
        activity(registration, player, config, classification, message_id)
    activity(registration, player, config, NetworkActivity.Classification.VALID, 9, active=False)
    resolved = resolve_effective_game_config(BASE)
    _, facts, achievements, medals = reconcile_profile(player, resolved, BASE)
    assert facts.valid_verified_activity == 0
    assert "first-verified-activity" not in achievements
    assert "activity-veteran" not in medals


@pytest.mark.django_db
def test_customization_authorizes_only_current_players_entitlements(
    phase4_config_factory, player_factory
) -> None:
    config = phase4_config_factory()
    player = player_factory(630)
    other = player_factory(631)
    PlayerProgression.objects.create(
        player=player, lifetime_points=59800, level=20, dexo_stage="vanguard", config_version=config
    )
    own_unlock = PlayerAchievementUnlock.objects.create(
        player=player,
        achievement_key="first-mission",
        unlocked_at=BASE,
        config_version=config,
        evidence_category="TEST",
    )
    own_medal = PlayerMedalAward.objects.create(
        player=player,
        medal_key="season-founding",
        awarded_at=BASE,
        config_version=config,
        reason="TEST",
    )
    PlayerAchievementUnlock.objects.create(
        player=other,
        achievement_key="first-referral",
        unlocked_at=BASE,
        config_version=config,
        evidence_category="TEST",
    )
    resolved = resolve_effective_game_config(BASE)
    profile = update_customization(
        player,
        resolved,
        BASE,
        active_frame_key="premium",
        active_title_key="mission-runner",
        featured_medal_key=own_medal.medal_key,
        featured_achievement_key=own_unlock.achievement_key,
    )
    retry = update_customization(
        player,
        resolved,
        BASE,
        active_frame_key="premium",
        active_title_key="mission-runner",
        featured_medal_key=own_medal.medal_key,
        featured_achievement_key=own_unlock.achievement_key,
    )
    assert retry.pk == profile.pk and retry.active_frame_key == "premium"
    for changes in (
        {"active_frame_key": "captain"},
        {"active_title_key": "network-builder"},
        {"featured_medal_key": "season-veteran"},
        {"featured_achievement_key": "first-war"},
    ):
        values = {
            "active_frame_key": "standard",
            "active_title_key": "explorer",
            "featured_medal_key": None,
            "featured_achievement_key": None,
            **changes,
        }
        with pytest.raises(ValidationError):
            update_customization(
                player,
                resolved,
                BASE,
                active_frame_key=values["active_frame_key"] or "",
                active_title_key=values["active_title_key"] or "",
                featured_medal_key=values["featured_medal_key"],
                featured_achievement_key=values["featured_achievement_key"],
            )


@pytest.mark.django_db
def test_complete_profile_review_and_normal_mission_claim(
    phase4_config_factory, player_factory, season_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config)
    player = player_factory(640)
    definition = mission("complete-profile")
    assert isinstance(definition, MissionDefinition)
    start_mission(player, definition.key, BASE)
    assert verify_mission(player, definition.key, BASE).status == PlayerMission.Status.IN_PROGRESS
    reviewed = review_profile(player, BASE)
    first_review = reviewed.first_reviewed_at
    assert review_profile(player, BASE + timedelta(hours=1)).first_reviewed_at == first_review
    assert RewardLedger.objects.count() == 0
    ready = verify_mission(player, definition.key, BASE)
    assert ready.status == PlayerMission.Status.REWARD_READY
    claimed, created = claim_mission(player, definition.key, BASE)
    retry, retry_created = claim_mission(player, definition.key, BASE)
    assert created and not retry_created and retry.pk == claimed.pk
    configured_reward = next(
        item["reward_points"]
        for item in config.payload["missions"]["initial_definitions"]
        if item["id"] == "complete-profile"
    )
    assert claimed.reward_amount == configured_reward
    assert RewardLedger.objects.filter(player=player, source_type="MISSION").count() == 1


def init_data(token: str, user_id: int) -> str:
    values = {
        "auth_date": str(int(timezone.now().timestamp())),
        "query_id": "profile-query",
        "user": json.dumps({"id": user_id, "first_name": "Profile"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.mark.django_db
def test_profile_api_auth_schema_and_safe_surface(
    settings: Any, client: Any, phase4_config_factory, season_factory
) -> None:
    settings.TELEGRAM_BOT_TOKEN = "123:profile"
    now = timezone.now()
    config = phase4_config_factory(effective_at=now - timedelta(days=1))
    season_factory(config, starts_at=now - timedelta(hours=1))
    paths = ["overview", "review", "achievements", "collection", "customization"]
    for path in paths:
        method = (
            client.put
            if path == "customization"
            else client.post
            if path == "review"
            else client.get
        )
        assert method(f"/api/v1/profile/{path}").status_code == 401
    auth = {"HTTP_AUTHORIZATION": f"tma {init_data(settings.TELEGRAM_BOT_TOKEN, 650)}"}
    overview = client.get("/api/v1/profile/overview", **auth)
    assert overview.status_code == 200
    assert "wallet" not in overview.json() and "dalexio_account" not in overview.json()
    invalid = client.put(
        "/api/v1/profile/customization",
        data=json.dumps({"active_frame": "captain", "active_title": "explorer"}),
        content_type="application/json",
        **auth,
    )
    assert invalid.status_code == 422
    from dexo_land.api import api

    schema = api.get_openapi_schema()
    for path in paths:
        assert f"/api/v1/profile/{path}" in schema["paths"]
