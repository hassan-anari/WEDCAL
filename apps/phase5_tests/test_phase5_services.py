import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import parse_phase5, publish_game_config, resolve_game_config_version
from apps.network.models import CreatorGroupRegistration, NetworkActivity
from apps.network.services import (
    purge_activity_evidence,
    record_activity,
    settle_completed_windows,
    window_start_for,
)
from apps.players.models import Player
from apps.progression.models import PlayerProgression
from apps.referrals.models import ReferralRelation, ReferralShareOutcome
from apps.referrals.services import (
    activate_relation,
    attribute_start,
    get_or_create_code,
    process_source_reward,
)
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
from apps.rewards.services import settle_player_reward
from apps.rewards.tasks import consume_outbox
from apps.seasons.models import Season
from apps.seasons.services import activate_season
from apps.telegram.models import TelegramGroup

AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
BASE = json.loads(Path("config/examples/season1.game-config.v1.json").read_text())


@pytest.fixture
def world(db, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)
    config = GameConfigVersion.objects.create(
        version="phase5-tests", effective_at=AT - timedelta(days=1), payload=copy.deepcopy(BASE)
    )
    publish_game_config(config, published_at=AT - timedelta(days=2))
    season = activate_season(
        Season.objects.create(
            code="p5",
            title="Phase 5",
            starts_at=AT - timedelta(days=1),
            ends_at=AT + timedelta(days=30),
            baseline_config=config,
        )
    )

    def player(telegram_id: int, *, age_days: int = 3) -> Player:
        return Player.objects.create(
            telegram_user_id=telegram_id,
            first_seen_at=AT - timedelta(days=age_days),
            last_seen_at=AT,
            first_seen_source=Player.FirstSeenSource.GROUP_ACTIVITY,
        )

    return config, season, player


def disabled_payload() -> dict[str, object]:
    payload = copy.deepcopy(BASE)
    payload["referral"]["enabled"] = False
    payload["network_activity"]["enabled"] = False
    return cast(dict[str, object], payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("referral", "first_touch_immutable"), False),
        (("referral", "self_referral_allowed"), True),
        (("referral", "share_base"), "GROSS"),
        (("referral", "share_is_deducted_from_invitee"), True),
        (("referral", "recursive_referral_rewards_forbidden"), False),
        (("network_activity", "creator_reward", "creator_self_activity_eligible"), True),
        (("anti_fraud", "network", "suspicious_is_auto_ban"), True),
    ],
)
def test_phase5_config_rejects_unsafe_contract(path: tuple[str, ...], value: object) -> None:
    payload = copy.deepcopy(BASE)
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        parse_phase5(payload)


@pytest.mark.django_db
def test_referral_code_is_opaque_unique_and_immutable(world) -> None:  # type: ignore[no-untyped-def]
    _, _, player = world
    code = get_or_create_code(player(101))
    assert code == get_or_create_code(code.player)
    assert str(code.player.telegram_user_id) not in code.code
    code.code = "replacement"
    with pytest.raises(ValueError):
        code.save()


@pytest.mark.django_db
def test_first_touch_and_self_referral(world) -> None:  # type: ignore[no-untyped-def]
    _, _, player = world
    inviter, competing, invitee = player(201), player(202), player(203)
    first = attribute_start(invitee, get_or_create_code(inviter).code, at=AT)
    second = attribute_start(invitee, get_or_create_code(competing).code, at=AT)
    assert first is not None and second is not None
    assert second.inviter_id == inviter.pk
    assert attribute_start(inviter, get_or_create_code(inviter).code, at=AT) is None


@pytest.mark.django_db
def test_disabled_referral_and_network_fail_closed(world) -> None:  # type: ignore[no-untyped-def]
    config, _, player = world
    config.payload = disabled_payload()
    # Published rows are immutable, so use queryset update to emulate a historical fixture snapshot.
    GameConfigVersion.objects.filter(pk=config.pk).update(payload=config.payload)
    inviter, invitee = player(301), player(302)
    assert attribute_start(invitee, get_or_create_code(inviter).code, at=AT) is None
    resolved = resolve_game_config_version(config.pk)
    assert not resolved.referral.enabled and not resolved.network.enabled


@pytest.mark.django_db
def test_reward_outbox_is_atomic_and_duplicate_safe(world) -> None:  # type: ignore[no-untyped-def]
    config, _, player = world
    recipient = player(401)
    resolved = resolve_game_config_version(config.pk)
    first, created = settle_player_reward(
        player=recipient,
        source_type="DAILY",
        source_id="day",
        context="DAILY",
        amount=100,
        idempotency_key="p5:outbox",
        occurred_at=AT,
        resolved_config=resolved,
    )
    second, duplicate = settle_player_reward(
        player=recipient,
        source_type="DAILY",
        source_id="day",
        context="DAILY",
        amount=100,
        idempotency_key="p5:outbox",
        occurred_at=AT,
        resolved_config=resolved,
    )
    assert created and not duplicate and first == second
    assert RewardSettlementOutbox.objects.filter(reward=first).count() == 1


@pytest.mark.django_db
def test_outbox_live_lease_and_stale_lease_recovery(world) -> None:  # type: ignore[no-untyped-def]
    config, _, player = world
    reward, _ = settle_player_reward(
        player=player(501),
        source_type="DAILY",
        source_id="lease",
        context="DAILY",
        amount=10,
        idempotency_key="p5:lease",
        occurred_at=AT,
        resolved_config=resolve_game_config_version(config.pk),
    )
    outbox = reward.outbox
    outbox.state = RewardSettlementOutbox.State.PROCESSING
    outbox.lease_until = timezone.now() + timedelta(minutes=1)
    outbox.save()
    assert consume_outbox(outbox.pk) is False
    outbox.lease_until = timezone.now() - timedelta(seconds=1)
    outbox.save(update_fields=("lease_until",))
    assert consume_outbox(outbox.pk) is True
    outbox.refresh_from_db()
    assert outbox.state == RewardSettlementOutbox.State.PROCESSED


@pytest.mark.django_db
def test_network_boundaries_duplicates_self_and_retention(world) -> None:  # type: ignore[no-untyped-def]
    config, season, player = world
    creator, speaker = player(601), player(602)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-601,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=AT,
        last_seen_at=AT,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator, group=group, state=CreatorGroupRegistration.State.ACTIVE
    )
    short = record_activity(
        registration=registration,
        sender=speaker,
        message_id=1,
        update_id=1,
        text="x" * 19,
        observed_at=AT,
    )
    valid = record_activity(
        registration=registration,
        sender=speaker,
        message_id=2,
        update_id=2,
        text="x" * 201,
        observed_at=AT + timedelta(seconds=1),
    )
    own = record_activity(
        registration=registration,
        sender=creator,
        message_id=3,
        update_id=3,
        text="creator message long",
        observed_at=AT + timedelta(seconds=2),
    )
    duplicate = record_activity(
        registration=registration,
        sender=speaker,
        message_id=4,
        update_id=4,
        text="x" * 201,
        observed_at=AT + timedelta(minutes=6),
    )
    assert short and short.rewardable_chars == 0
    assert valid and valid.rewardable_chars == 200 and valid.season_id == season.pk
    assert own and own.rewardable_chars == 0
    assert duplicate and duplicate.classification == NetworkActivity.Classification.SUSPICIOUS
    purge_activity_evidence(AT + timedelta(days=100))
    valid.refresh_from_db()
    assert valid.text is None and valid.exact_fingerprint == "" and valid.rewardable_chars == 200


def test_window_identity_is_deterministic() -> None:
    assert window_start_for(AT + timedelta(minutes=14, seconds=59), 900) == window_start_for(
        AT, 900
    )


@pytest.mark.django_db
def test_activation_is_two_sided_atomic_and_moderation_gated(world) -> None:  # type: ignore[no-untyped-def]
    config, _, player = world
    inviter, invitee = player(701), player(702)
    relation = attribute_start(invitee, get_or_create_code(inviter).code, at=AT)
    assert relation
    PlayerProgression.objects.create(
        player=invitee, lifetime_points=2500, level=5, dexo_stage="explorer", config_version=config
    )
    invitee.moderation_state = Player.ModerationState.SUSPENDED
    invitee.save(update_fields=("moderation_state",))
    assert activate_relation(relation.pk, at=AT) is False
    assert RewardLedger.objects.count() == 0
    invitee.moderation_state = Player.ModerationState.ACTIVE
    invitee.save(update_fields=("moderation_state",))
    assert activate_relation(relation.pk, at=AT + timedelta(hours=2)) is True
    assert sorted(RewardLedger.objects.values_list("player_amount", flat=True)) == [50, 100]


@pytest.mark.django_db
def test_passive_share_exact_once_and_runtime_non_recursion(world) -> None:  # type: ignore[no-untyped-def]
    config, season, player = world
    inviter, invitee = player(801), player(802)
    relation = ReferralRelation.objects.create(
        inviter=inviter,
        invitee=invitee,
        status=ReferralRelation.Status.VERIFIED,
        attributed_at=AT - timedelta(days=2),
        verified_at=AT - timedelta(days=1),
        verified_config=config,
    )
    source = RewardLedger.objects.create(
        player=invitee,
        season=season,
        source_type="DAILY",
        source_id="share-source",
        context="DAILY",
        gross_amount=100,
        player_amount=100,
        config_version=config,
        idempotency_key="share-source",
        occurred_at=AT,
    )
    first = process_source_reward(source)
    assert process_source_reward(source).pk == first.pk and first.amount == 3
    recursive = RewardLedger.objects.create(
        player=invitee,
        season=season,
        source_type="CREATOR",
        source_id="no-recursion",
        context="NETWORK_GROUP",
        gross_amount=100,
        player_amount=100,
        config_version=config,
        idempotency_key="no-recursion",
        occurred_at=AT,
    )
    assert process_source_reward(recursive).amount == 0
    assert ReferralShareOutcome.objects.filter(relation=relation, amount__gt=0).count() == 1


@pytest.mark.django_db
def test_creator_scheduler_settles_600_chars_with_two_speakers_once(world) -> None:  # type: ignore[no-untyped-def]
    _, _, player = world
    creator, first, second = player(901), player(902), player(903)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-901,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=AT,
        last_seen_at=AT,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator, group=group, state=CreatorGroupRegistration.State.ACTIVE
    )
    for message_id, sender in enumerate((first, first, second), start=1):
        activity = record_activity(
            registration=registration,
            sender=sender,
            message_id=message_id,
            update_id=message_id,
            text=str(message_id) * 200,
            observed_at=AT + timedelta(seconds=message_id),
        )
        assert activity and activity.rewardable_chars == 200
    assert settle_completed_windows(now=AT + timedelta(minutes=16)) == 1
    assert RewardLedger.objects.get(source_type="CREATOR").player_amount == 1
    assert settle_completed_windows(now=AT + timedelta(minutes=17)) == 0
