# mypy: disable-error-code="no-untyped-def"
from datetime import UTC, datetime
from threading import Barrier, Thread

import pytest
from django.db import close_old_connections

from apps.game_config.services import resolve_effective_game_config
from apps.missions.models import MissionDefinition, PlayerMission
from apps.profile_collection.models import PlayerAchievementUnlock, PlayerMedalAward, PlayerProfile
from apps.profile_collection.services import reconcile_profile, review_profile, update_customization
from apps.progression.models import PlayerProgression

BASE = datetime(2026, 1, 2, tzinfo=UTC)


def concurrently(*operations) -> None:
    barrier = Barrier(len(operations))
    errors: list[BaseException] = []

    def run(operation):
        close_old_connections()
        try:
            barrier.wait()
            operation()
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=run, args=(operation,)) for operation in operations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors


@pytest.mark.django_db(transaction=True)
def test_concurrent_review_and_reconciliation_preserve_first_review(
    phase4_config_factory, player_factory
) -> None:
    phase4_config_factory()
    player = player_factory(701)
    PlayerProfile.objects.create(player=player, active_frame_key="founder")
    resolved = resolve_effective_game_config(BASE)
    concurrently(
        lambda: reconcile_profile(player, resolved, BASE),
        lambda: review_profile(player, BASE),
    )
    profile = PlayerProfile.objects.get(player=player)
    assert profile.first_reviewed_at == BASE
    assert profile.active_frame_key == "standard"


@pytest.mark.django_db(transaction=True)
def test_reconciliation_fallback_cannot_overwrite_new_valid_customization(
    phase4_config_factory, player_factory
) -> None:
    def tune(data):
        data["season_honors"]["medals"]["mission-master"]["min_claimed_missions"] = 1

    config = phase4_config_factory(mutate=tune)
    player = player_factory(702)
    PlayerProgression.objects.create(
        player=player,
        lifetime_points=59800,
        level=20,
        dexo_stage="vanguard",
        config_version=config,
    )
    PlayerProfile.objects.create(player=player, active_frame_key="founder")
    resolved = resolve_effective_game_config(BASE)
    concurrently(
        lambda: reconcile_profile(player, resolved, BASE),
        lambda: update_customization(
            player,
            resolved,
            BASE,
            active_frame_key="premium",
            active_title_key="explorer",
            featured_medal_key=None,
            featured_achievement_key=None,
        ),
    )
    assert PlayerProfile.objects.get(player=player).active_frame_key == "premium"


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_achievement_reconciliation_is_idempotent(
    phase4_config_factory, player_factory
) -> None:
    def tune(data):
        data["season_honors"]["medals"]["mission-master"]["min_claimed_missions"] = 1

    config = phase4_config_factory(mutate=tune)
    player = player_factory(703)
    definition = MissionDefinition.objects.create(
        key="fact",
        version=1,
        enabled=False,  # Historical achievement evidence need not be an enabled live mission.
        verification_family=MissionDefinition.Verification.INTERNAL,
        repeat_policy=MissionDefinition.Repeat.ONCE,
    )
    PlayerMission.objects.create(
        player=player,
        definition=definition,
        eligibility_period="definition",
        status=PlayerMission.Status.CLAIMED,
        claimed_at=BASE,
        config_version=config,
    )
    PlayerProfile.objects.create(player=player)
    resolved = resolve_effective_game_config(BASE)
    concurrently(
        lambda: reconcile_profile(player, resolved, BASE),
        lambda: reconcile_profile(player, resolved, BASE),
    )
    assert (
        PlayerAchievementUnlock.objects.filter(
            player=player, achievement_key="first-mission"
        ).count()
        == 1
    )
    assert PlayerMedalAward.objects.filter(player=player, medal_key="mission-master").count() == 1
