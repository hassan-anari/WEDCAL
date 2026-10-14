from datetime import datetime
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.game_config.services import ResolvedGameConfig, resolve_effective_game_config
from apps.players.avatars import avatar_url
from apps.players.models import Player
from apps.profile_collection.models import PlayerAchievementUnlock, PlayerMedalAward, PlayerProfile
from apps.profile_collection.services import (
    ProfileFacts,
    achievement_progress,
    entitlement,
    reconcile_profile,
    review_profile,
    update_customization,
)
from apps.progression.models import PlayerProgression
from apps.progression.services import calculate_progression
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["profile"], auth=mini_app_auth)


class IdentityOut(Schema):
    first_name: str
    last_name: str
    username: str
    photo_url: str


class ProgressionOut(Schema):
    lifetime_points: int
    level: int
    current_threshold: int
    next_threshold: int | None
    points_into_level: int
    points_remaining: int | None
    dexo_stage: str


class JourneyOut(Schema):
    claimed_missions: int
    current_daily_streak: int
    verified_referrals: int


class CustomizationOut(Schema):
    active_frame: str
    active_title: str
    featured_medal: str | None
    featured_achievement: str | None


class OverviewOut(Schema):
    identity: IdentityOut
    progression: ProgressionOut
    journey: JourneyOut
    customization: CustomizationOut


class AchievementOut(Schema):
    key: str
    available_s1: bool
    unlocked: bool
    unlocked_at: datetime | None
    current_progress: int
    target: int
    point_reward: int = 0


class AchievementsOut(Schema):
    achievements: list[AchievementOut]
    unlocked_count: int


class CollectionItemOut(Schema):
    key: str
    available: bool
    entitled: bool
    active: bool


class MedalOut(Schema):
    key: str
    group: str
    owned: bool
    featured: bool
    awarded_at: datetime | None
    source_season: str | None


class CollectionOut(Schema):
    frames: list[CollectionItemOut]
    titles: list[CollectionItemOut]
    medals: list[MedalOut]
    owned_medal_count: int


class CustomizationIn(Schema):
    active_frame: str
    active_title: str
    featured_medal: str | None = None
    featured_achievement: str | None = None


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    return player


def _state(
    player: Player,
) -> tuple[
    datetime,
    ResolvedGameConfig,
    tuple[
        PlayerProfile,
        ProfileFacts,
        dict[str, PlayerAchievementUnlock],
        dict[str, PlayerMedalAward],
    ],
]:
    at = timezone.now()
    resolved = resolve_effective_game_config(at)
    try:
        return at, resolved, reconcile_profile(player, resolved, at)
    except ValidationError as exc:
        raise HttpError(503, str(exc.message)) from exc


def _customization(profile: PlayerProfile) -> CustomizationOut:
    return CustomizationOut(
        active_frame=profile.active_frame_key,
        active_title=profile.active_title_key,
        featured_medal=profile.featured_medal_key,
        featured_achievement=profile.featured_achievement_key,
    )


def _source_season_code(award: PlayerMedalAward | None) -> str | None:
    if award is None or award.source_season is None:
        return None
    return award.source_season.code


@router.get("/overview", response=OverviewOut)
def overview(request: HttpRequest) -> OverviewOut:
    player = _player(request)
    _, resolved, (profile, facts, _, _) = _state(player)
    projection = PlayerProgression.objects.get(player=player)
    progress = calculate_progression(projection.lifetime_points, resolved.progression)
    return OverviewOut(
        identity=IdentityOut(
            first_name=player.first_name,
            last_name=player.last_name,
            username=player.username,
            photo_url=avatar_url(player),
        ),
        progression=ProgressionOut(
            lifetime_points=projection.lifetime_points,
            level=progress.level,
            current_threshold=progress.current_threshold,
            next_threshold=progress.next_threshold,
            points_into_level=progress.points_into_level,
            points_remaining=progress.points_remaining,
            dexo_stage=progress.dexo_stage,
        ),
        journey=JourneyOut(
            claimed_missions=facts.claimed_missions,
            current_daily_streak=facts.current_daily_streak,
            verified_referrals=facts.verified_referrals,
        ),
        customization=_customization(profile),
    )


@router.post("/review", response=CustomizationOut)
def review(request: HttpRequest) -> CustomizationOut:
    return _customization(review_profile(_player(request), timezone.now()))


@router.get("/achievements", response=AchievementsOut)
def achievements(request: HttpRequest) -> AchievementsOut:
    _, resolved, (_, facts, unlocks, _) = _state(_player(request))
    assert resolved.profile_collection is not None
    rows = [
        AchievementOut(
            key=rule.key,
            available_s1=rule.available_s1,
            unlocked=rule.key in unlocks,
            unlocked_at=unlocks[rule.key].unlocked_at if rule.key in unlocks else None,
            current_progress=achievement_progress(rule.key, facts),
            target=rule.target,
        )
        for rule in resolved.profile_collection.achievements
    ]
    return AchievementsOut(achievements=rows, unlocked_count=len(unlocks))


@router.get("/collection", response=CollectionOut)
def collection(request: HttpRequest) -> CollectionOut:
    _, resolved, (profile, facts, unlocks, awards) = _state(_player(request))
    assert resolved.profile_collection is not None and resolved.season_honors is not None
    achievement_keys, medal_keys = set(unlocks), set(awards)
    frames = [
        CollectionItemOut(
            key=x.key,
            available=True,
            entitled=entitlement(x, facts, achievement_keys, medal_keys),
            active=profile.active_frame_key == x.key,
        )
        for x in resolved.profile_collection.frames
    ]
    titles = [
        CollectionItemOut(
            key=x.key,
            available=True,
            entitled=entitlement(x, facts, achievement_keys, medal_keys),
            active=profile.active_title_key == x.key,
        )
        for x in resolved.profile_collection.titles
    ]
    season_keys = {
        "season-founding",
        "season-participant",
        "season-veteran",
        "season-top-10",
        "season-top-1",
        "season-champion",
    }
    medals = [
        MedalOut(
            key=x.key,
            group="SEASON" if x.key in season_keys else "CAREER",
            owned=x.key in awards,
            featured=profile.featured_medal_key == x.key,
            awarded_at=awards[x.key].awarded_at if x.key in awards else None,
            source_season=_source_season_code(awards.get(x.key)),
        )
        for x in resolved.season_honors.medals
    ]
    return CollectionOut(frames=frames, titles=titles, medals=medals, owned_medal_count=len(awards))


@router.put("/customization", response=CustomizationOut)
def customization(request: HttpRequest, payload: CustomizationIn) -> CustomizationOut:
    at = timezone.now()
    resolved = resolve_effective_game_config(at)
    try:
        profile = update_customization(
            _player(request),
            resolved,
            at,
            active_frame_key=payload.active_frame,
            active_title_key=payload.active_title,
            featured_medal_key=payload.featured_medal,
            featured_achievement_key=payload.featured_achievement,
        )
    except ValidationError as exc:
        raise HttpError(422, str(exc.message)) from exc
    return _customization(profile)


@router.get("/avatar/{delivery_id}", auth=None)
def avatar_delivery(request: HttpRequest, delivery_id: UUID) -> HttpResponse:
    from apps.players.avatars import deliver_avatar

    return deliver_avatar(delivery_id)
