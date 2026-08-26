import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from django.db import close_old_connections

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import publish_game_config
from apps.players.models import Player
from apps.referrals.models import ReferralRelation
from apps.referrals.services import attribute_start, get_or_create_code

AT = datetime(2026, 8, 5, 12, tzinfo=UTC)
BASE = json.loads(Path("config/examples/season1.game-config.v1.json").read_text())


@pytest.mark.django_db(transaction=True)
def test_concurrent_two_node_referral_cycle_is_prevented() -> None:
    config = GameConfigVersion.objects.create(
        version="phase5-cycle", effective_at=AT - timedelta(days=1), payload=copy.deepcopy(BASE)
    )
    publish_game_config(config, published_at=AT - timedelta(days=2))
    first = Player.objects.create(
        telegram_user_id=9901,
        first_seen_at=AT - timedelta(days=2),
        last_seen_at=AT,
        first_seen_source=Player.FirstSeenSource.MINI_APP,
    )
    second = Player.objects.create(
        telegram_user_id=9902,
        first_seen_at=AT - timedelta(days=2),
        last_seen_at=AT,
        first_seen_source=Player.FirstSeenSource.MINI_APP,
    )
    first_code = get_or_create_code(first).code
    second_code = get_or_create_code(second).code
    barrier = Barrier(2)

    def attribute(invitee_id: int, code: str) -> None:
        close_old_connections()
        try:
            barrier.wait()
            attribute_start(Player.objects.get(pk=invitee_id), code, at=AT)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(attribute, first.pk, second_code),
            pool.submit(attribute, second.pk, first_code),
        )
        for future in futures:
            future.result(timeout=10)

    assert ReferralRelation.objects.count() == 1


def make_config(version: str, *, mutate=None) -> GameConfigVersion:  # type: ignore[no-untyped-def]
    payload = copy.deepcopy(BASE)
    if mutate:
        mutate(payload)
    config = GameConfigVersion.objects.create(
        version=version, effective_at=AT - timedelta(days=1), payload=payload
    )
    return publish_game_config(config, published_at=AT - timedelta(days=2))


def make_player(telegram_id: int) -> Player:
    return Player.objects.create(
        telegram_user_id=telegram_id,
        first_seen_at=AT - timedelta(days=2),
        last_seen_at=AT,
        first_seen_source=Player.FirstSeenSource.MINI_APP,
    )


def run_parallel(calls) -> None:  # type: ignore[no-untyped-def]
    barrier = Barrier(len(calls))

    def invoke(call) -> None:  # type: ignore[no-untyped-def]
        close_old_connections()
        try:
            barrier.wait()
            call()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(invoke, call) for call in calls]
        for future in futures:
            future.result(timeout=15)


@pytest.mark.django_db(transaction=True)
def test_concurrent_referral_activation_settles_bonuses_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.progression.models import PlayerProgression
    from apps.referrals.services import activate_relation
    from apps.rewards.models import RewardLedger
    from apps.seasons.models import Season
    from apps.seasons.services import activate_season

    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)
    config = make_config("phase5-activation-race")
    activate_season(
        Season.objects.create(
            code="activation-race",
            title="Activation",
            starts_at=AT - timedelta(days=1),
            ends_at=AT + timedelta(days=10),
            baseline_config=config,
        )
    )
    inviter, invitee = make_player(9911), make_player(9912)
    relation = attribute_start(invitee, get_or_create_code(inviter).code, at=AT)
    assert relation
    PlayerProgression.objects.create(
        player=invitee,
        lifetime_points=2500,
        level=5,
        dexo_stage="explorer",
        config_version=config,
    )
    run_parallel(
        [
            lambda: activate_relation(relation.pk, at=AT),
            lambda: activate_relation(relation.pk, at=AT),
        ]
    )
    assert RewardLedger.objects.filter(source_type="REFERRAL").count() == 2
    assert sorted(RewardLedger.objects.values_list("player_amount", flat=True)) == [50, 100]


@pytest.mark.django_db(transaction=True)
def test_concurrent_creator_group_activation_never_exceeds_three(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.network.models import CreatorGroupRegistration
    from apps.network.services import verify_registration
    from apps.telegram.bot_api import TelegramResult
    from apps.telegram.models import TelegramGroup

    make_config("phase5-group-cap-race")
    creator = make_player(9921)
    registrations = []
    for number in range(4):
        group = TelegramGroup.objects.create(
            telegram_chat_id=-99210 - number,
            chat_type="supergroup",
            first_seen_at=AT,
            last_seen_at=AT,
        )
        registrations.append(CreatorGroupRegistration.objects.create(creator=creator, group=group))
    monkeypatch.setattr(
        "apps.network.services.get_chat_member",
        lambda *_args: TelegramResult(True, {"status": "administrator"}),
    )
    monkeypatch.setattr("apps.network.services.settings.TELEGRAM_BOT_ID", 99999)
    monkeypatch.setattr("apps.network.services.timezone.now", lambda: AT)
    run_parallel([lambda pk=item.pk: verify_registration(pk) for item in registrations])
    assert (
        CreatorGroupRegistration.objects.filter(
            creator=creator, state=CreatorGroupRegistration.State.ACTIVE
        ).count()
        == 3
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_two_group_settlement_respects_creator_daily_cap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.network.models import CreatorGroupRegistration, NetworkActivity
    from apps.network.services import settle_window
    from apps.rewards.models import RewardLedger
    from apps.seasons.models import Season
    from apps.seasons.services import activate_season
    from apps.telegram.models import TelegramGroup

    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)

    def cap(payload) -> None:  # type: ignore[no-untyped-def]
        payload["network_activity"]["creator_reward"]["max_points_per_creator_per_day"] = 3

    config = make_config("phase5-daily-cap-race", mutate=cap)
    season = activate_season(
        Season.objects.create(
            code="daily-cap-race",
            title="Daily cap",
            starts_at=AT - timedelta(days=1),
            ends_at=AT + timedelta(days=10),
            baseline_config=config,
        )
    )
    creator, first, second = make_player(9931), make_player(9932), make_player(9933)
    registrations = []
    for number in range(2):
        group = TelegramGroup.objects.create(
            telegram_chat_id=-99310 - number,
            chat_type="supergroup",
            is_monitored=True,
            first_seen_at=AT,
            last_seen_at=AT,
        )
        registration = CreatorGroupRegistration.objects.create(
            creator=creator, group=group, state=CreatorGroupRegistration.State.ACTIVE
        )
        registrations.append(registration)
        for offset, sender in enumerate((first, second)):
            NetworkActivity.objects.create(
                registration=registration,
                group=group,
                sender=sender,
                telegram_message_id=offset + 1,
                telegram_update_id=number * 10 + offset,
                observed_at=AT + timedelta(seconds=offset),
                character_count=600,
                rewardable_chars=600,
                classification=NetworkActivity.Classification.VALID,
                config_version=config,
                season=season,
                program_active=True,
                content_expires_at=AT + timedelta(days=14),
                fingerprint_expires_at=AT + timedelta(days=90),
            )
    run_parallel(
        [
            lambda registration=item: settle_window(registration.pk, AT, config.pk, season.pk)
            for item in registrations
        ]
    )
    assert (
        sum(
            RewardLedger.objects.filter(source_type="CREATOR").values_list(
                "player_amount", flat=True
            )
        )
        == 3
    )
