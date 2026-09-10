import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import (
    parse_profile_collection,
    publish_game_config,
    resolve_effective_game_config,
)
from apps.players.models import Player
from apps.profile_collection.models import PlayerAchievementUnlock, PlayerMedalAward
from apps.profile_collection.services import reconcile_profile, review_profile, update_customization


def payload() -> dict[str, object]:
    path = Path(__file__).parents[3] / "config/examples/season1.game-config.v1.json"
    return cast(dict[str, object], json.loads(path.read_text()))


def player() -> Player:
    now = timezone.now()
    return Player.objects.create(
        telegram_user_id=9001,
        first_name="Dexo",
        first_seen_at=now,
        last_seen_at=now,
        first_seen_source=Player.FirstSeenSource.MINI_APP,
    )


def config() -> GameConfigVersion:
    return GameConfigVersion.objects.create(
        version="phase6-test",
        schema_version=1,
        status=GameConfigVersion.Status.PUBLISHED,
        effective_at=timezone.now() - timedelta(days=1),
        payload=payload(),
        published_at=timezone.now(),
    )


def test_profile_config_parses_canonical_catalogs() -> None:
    profile, honors = parse_profile_collection(payload())
    assert len(profile.achievements) == 10
    assert len(profile.frames) == 6
    assert len(profile.titles) == 6
    assert len(honors.medals) == 10


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("profile_collection", "achievement_point_rewards_enabled"), True),
        (("profile_collection", "achievements", "first-mission", "point_reward"), 1),
        (("profile_collection", "achievements", "first-war", "available_s1"), True),
        (("profile_collection", "achievements", "first-verified-activity", "target"), 200),
        (("profile_collection", "frames", "standard", "unlock"), "LEVEL:1"),
        (("profile_collection", "titles", "explorer", "unlock"), "NOPE"),
        (("season_honors", "point_rewards_enabled"), True),
    ],
)
def test_profile_config_rejects_unsafe_rules(path: tuple[str, ...], value: object) -> None:
    data = copy.deepcopy(payload())
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        parse_profile_collection(data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("profile_collection", "frames", "captain", "unlock"), "DEFAULT"),
        (("profile_collection", "frames", "clan", "unlock"), "DEFAULT"),
        (("profile_collection", "frames", "founder", "unlock"), "LEVEL:5"),
        (("profile_collection", "titles", "mission-runner", "unlock"), "DEFAULT"),
        (("profile_collection", "achievements", "first-mission"), []),
        (("profile_collection", "frames", "standard"), "DEFAULT"),
        (("profile_collection", "titles", "explorer"), None),
        (("season_honors", "medals", "mission-master"), 50),
        (("season_honors", "medals", "season-top-1", "max_rank_percentile_bps"), 10001),
        (("season_honors", "medals", "season-top-1", "max_rank_percentile_bps"), 2000),
        (("season_honors", "medals", "season-participant", "min_season_points"), 40000),
        (("clan", "unlock", "min_level"), 6),
    ],
)
def test_profile_config_rejects_semantic_authorization_changes(
    path: tuple[str, ...], value: object
) -> None:
    data = copy.deepcopy(payload())
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        parse_profile_collection(data)


@pytest.mark.parametrize(("catalog", "key"), [("frames", "bonus"), ("titles", "hero")])
def test_profile_config_rejects_extra_catalog_keys(catalog: str, key: str) -> None:
    data = copy.deepcopy(payload())
    data["profile_collection"][catalog][key] = {"unlock": "DEFAULT"}  # type: ignore[index]
    with pytest.raises(ValidationError):
        parse_profile_collection(data)


def test_profile_config_catalog_order_is_canonical() -> None:
    data = copy.deepcopy(payload())
    frames = data["profile_collection"]["frames"]  # type: ignore[index]
    data["profile_collection"]["frames"] = dict(reversed(list(frames.items())))  # type: ignore[index]
    profile, honors = parse_profile_collection(data)
    assert [item.key for item in profile.frames] == [
        "standard",
        "premium",
        "seasonal",
        "founder",
        "clan",
        "captain",
    ]
    assert [item.key for item in honors.medals][:3] == [
        "season-founding",
        "season-participant",
        "season-veteran",
    ]


@pytest.mark.django_db
def test_publication_rejects_unsafe_profile_entitlement() -> None:
    data = payload()
    unsafe: Any = data
    unsafe["profile_collection"]["frames"]["captain"]["unlock"] = "DEFAULT"
    draft = GameConfigVersion.objects.create(
        version="unsafe-profile",
        schema_version=1,
        effective_at=timezone.now() + timedelta(days=1),
        payload=data,
    )
    with pytest.raises(ValidationError):
        publish_game_config(draft)


@pytest.mark.django_db
def test_profile_defaults_reconcile_and_review_are_idempotent() -> None:
    row, cfg = player(), config()
    resolved = resolve_effective_game_config(timezone.now())
    profile, facts, achievements, medals = reconcile_profile(row, resolved, timezone.now())
    assert profile.active_frame_key == "standard"
    assert profile.active_title_key == "explorer"
    assert not achievements and not medals and facts.claimed_missions == 0
    first = review_profile(row, timezone.now())
    timestamp = first.first_reviewed_at
    second = review_profile(row, timezone.now() + timedelta(hours=1))
    assert second.first_reviewed_at == timestamp
    assert PlayerAchievementUnlock.objects.count() == 0
    assert PlayerMedalAward.objects.count() == 0
    assert cfg.pk == resolved.id


@pytest.mark.django_db
def test_customization_rejects_unowned_and_accepts_unlocked() -> None:
    row, cfg = player(), config()
    resolved = resolve_effective_game_config(timezone.now())
    with pytest.raises(ValidationError):
        update_customization(
            row,
            resolved,
            timezone.now(),
            active_frame_key="founder",
            active_title_key="explorer",
            featured_medal_key=None,
            featured_achievement_key=None,
        )
    unlock = PlayerAchievementUnlock.objects.create(
        player=row,
        achievement_key="first-mission",
        unlocked_at=timezone.now(),
        config_version=cfg,
        evidence_category="TEST",
    )
    profile = update_customization(
        row,
        resolved,
        timezone.now(),
        active_frame_key="standard",
        active_title_key="mission-runner",
        featured_medal_key=None,
        featured_achievement_key="first-mission",
    )
    assert profile.active_title_key == "mission-runner"
    assert profile.featured_achievement_key == unlock.achievement_key
