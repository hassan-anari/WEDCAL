import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import parse_phase5, publish_game_config, resolve_game_config_version
from apps.network.models import (
    CreatorGroupRegistration,
    CreatorWindowSettlement,
    NetworkActivity,
)
from apps.network.services import (
    purge_activity_evidence,
    record_activity,
    settle_completed_windows,
    verify_registration,
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
        schema_version=BASE["meta"]["schema_version"],
        version="phase5-tests",
        effective_at=AT - timedelta(days=1),
        payload=copy.deepcopy(BASE),
    )
    publish_game_config(config, published_at=AT - timedelta(days=2))
    season = activate_season(
        Season.objects.create(
            code="p5",
            title="Phase 5",
            starts_at=AT - timedelta(days=1),
            ends_at=AT + timedelta(days=44),
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
        creator=creator,
        group=group,
        state=CreatorGroupRegistration.State.ACTIVE,
        reward_eligible_since=AT - timedelta(days=1),
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
        creator=creator,
        group=group,
        state=CreatorGroupRegistration.State.ACTIVE,
        reward_eligible_since=AT - timedelta(days=1),
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
    assert settle_completed_windows(now=AT + timedelta(minutes=26)) == 1
    assert RewardLedger.objects.get(source_type="CREATOR").player_amount == 1
    assert settle_completed_windows(now=AT + timedelta(minutes=27)) == 0


@pytest.mark.django_db
def test_out_of_order_future_message_does_not_create_duplicate_signal(world) -> None:  # type: ignore[no-untyped-def]
    _, _, player = world
    creator, speaker = player(1001), player(1002)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1001,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=AT,
        last_seen_at=AT,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator,
        group=group,
        state=CreatorGroupRegistration.State.ACTIVE,
        reward_eligible_since=AT - timedelta(days=1),
    )
    newer = record_activity(
        registration=registration,
        sender=speaker,
        message_id=2,
        update_id=2,
        text="same normalized message",
        observed_at=AT + timedelta(minutes=4),
    )
    older = record_activity(
        registration=registration,
        sender=speaker,
        message_id=1,
        update_id=1,
        text="same normalized message",
        observed_at=AT,
    )
    assert newer and newer.classification == NetworkActivity.Classification.VALID
    assert older and older.classification == NetworkActivity.Classification.VALID


@pytest.mark.django_db
def test_late_activity_is_explicit_after_grace_finalization(world) -> None:  # type: ignore[no-untyped-def]
    _, _, player = world
    creator, first, second = player(1101), player(1102), player(1103)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1101,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=AT,
        last_seen_at=AT,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator,
        group=group,
        state=CreatorGroupRegistration.State.ACTIVE,
        reward_eligible_since=AT - timedelta(days=1),
    )
    for message_id, sender in ((1, first), (2, second)):
        record_activity(
            registration=registration,
            sender=sender,
            message_id=message_id,
            update_id=message_id,
            text="z" * 300,
            observed_at=AT + timedelta(seconds=message_id),
        )
    assert settle_completed_windows(now=AT + timedelta(minutes=26)) == 1
    late = record_activity(
        registration=registration,
        sender=first,
        message_id=3,
        update_id=3,
        text="late but durable message",
        observed_at=AT + timedelta(minutes=5),
    )
    assert late and late.classification == NetworkActivity.Classification.LATE
    assert late.rewardable_chars == 0 and late.window_processed


@pytest.mark.django_db
def test_closed_season_window_has_durable_deferred_outcome(world) -> None:  # type: ignore[no-untyped-def]
    config, season, player = world
    creator, speaker = player(1201), player(1202)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1201,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=AT,
        last_seen_at=AT,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator,
        group=group,
        state=CreatorGroupRegistration.State.ACTIVE,
        reward_eligible_since=AT - timedelta(days=1),
    )
    record_activity(
        registration=registration,
        sender=speaker,
        message_id=1,
        update_id=1,
        text="d" * 200,
        observed_at=AT,
    )
    Season.objects.filter(pk=season.pk).update(status=Season.Status.CLOSED)
    assert settle_completed_windows(now=AT + timedelta(minutes=26)) == 1
    settlement = CreatorWindowSettlement.objects.get(
        registration=registration, season=season, config_version=config
    )
    assert settlement.state == CreatorWindowSettlement.State.DEFERRED_SEASON_CLOSE
    assert settlement.reward is None and settlement.finalized_at is not None
    assert settle_completed_windows(now=AT + timedelta(minutes=27)) == 0


@pytest.mark.django_db
def test_terminal_creator_registration_cannot_be_reactivated(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, _, player = world
    creator = player(1301)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1301, chat_type="supergroup", first_seen_at=AT, last_seen_at=AT
    )
    for state in (
        CreatorGroupRegistration.State.SUSPENDED,
        CreatorGroupRegistration.State.INACTIVE,
    ):
        registration = CreatorGroupRegistration.objects.create(
            creator=creator,
            group=group
            if state == CreatorGroupRegistration.State.SUSPENDED
            else TelegramGroup.objects.create(
                telegram_chat_id=-1302, chat_type="supergroup", first_seen_at=AT, last_seen_at=AT
            ),
            state=state,
        )
        monkeypatch.setattr(
            "apps.network.services.get_chat_member", lambda *_args: pytest.fail("called")
        )
        assert verify_registration(registration.pk) == state
        registration.refresh_from_db()
        assert registration.state == state


@pytest.mark.django_db
def test_current_config_progression_curve_controls_referral_level(world) -> None:  # type: ignore[no-untyped-def]
    config, _, player = world
    inviter, invitee = player(1401), player(1402)
    relation = attribute_start(invitee, get_or_create_code(inviter).code, at=AT)
    assert relation
    PlayerProgression.objects.create(
        player=invitee, lifetime_points=2100, level=4, dexo_stage="explorer", config_version=config
    )
    payload = copy.deepcopy(BASE)
    payload["progression"]["levels"][4]["min_lifetime_points"] = 2000
    second = GameConfigVersion.objects.create(
        schema_version=BASE["meta"]["schema_version"],
        version="phase5-progression-v2",
        effective_at=AT - timedelta(hours=1),
        payload=payload,
    )
    publish_game_config(second, published_at=AT - timedelta(hours=2))
    assert activate_relation(relation.pk, at=AT) is True


@pytest.mark.django_db
def test_future_speaker_reward_is_deterministic_and_referral_ineligible(world) -> None:  # type: ignore[no-untyped-def]
    _, _, player = world
    payload = copy.deepcopy(BASE)
    payload["network_activity"]["speaker_reward"] = {"enabled": True, "points_per_unit": 7}
    future = GameConfigVersion.objects.create(
        schema_version=BASE["meta"]["schema_version"],
        version="phase5-speaker",
        effective_at=AT - timedelta(hours=1),
        payload=payload,
    )
    publish_game_config(future, published_at=AT - timedelta(hours=2))
    creator, speaker = player(1501), player(1502)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1501,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=AT,
        last_seen_at=AT,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator,
        group=group,
        state=CreatorGroupRegistration.State.ACTIVE,
        reward_eligible_since=AT - timedelta(days=1),
    )
    activity = record_activity(
        registration=registration,
        sender=speaker,
        message_id=1,
        update_id=1,
        text="eligible future speaker",
        observed_at=AT,
    )
    assert activity and activity.speaker_reward
    assert activity.speaker_reward.source_type == "NETWORK_SPEAKER"
    assert activity.speaker_reward.player_amount == 7
    assert (
        record_activity(
            registration=registration,
            sender=speaker,
            message_id=1,
            update_id=2,
            text="eligible future speaker",
            observed_at=AT,
        )
        is None
    )
    assert RewardLedger.objects.filter(source_type="NETWORK_SPEAKER").count() == 1


@pytest.mark.django_db
def test_referral_sweeper_advances_during_season_gap(world) -> None:  # type: ignore[no-untyped-def]
    _, season, player = world
    inviter, invitee = player(1601), player(1602)
    relation = attribute_start(invitee, get_or_create_code(inviter).code, at=AT)
    assert relation
    Season.objects.filter(pk=season.pk).update(status=Season.Status.CLOSED)
    assert activate_relation(relation.pk, at=AT) is False
    relation.refresh_from_db()
    assert relation.next_evaluation_at == AT + timedelta(hours=1)


@pytest.mark.django_db
def test_transient_creator_verification_persists_retry_schedule(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.telegram.bot_api import TelegramResult

    _, _, player = world
    creator = player(1701)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1701, chat_type="supergroup", first_seen_at=AT, last_seen_at=AT
    )
    registration = CreatorGroupRegistration.objects.create(creator=creator, group=group)
    monkeypatch.setattr("apps.network.services.settings.TELEGRAM_BOT_ID", 99999)
    monkeypatch.setattr("apps.network.services.timezone.now", lambda: AT)
    monkeypatch.setattr(
        "apps.network.services.get_chat_member",
        lambda *_args: TelegramResult(False, category="timeout"),
    )
    with pytest.raises(RuntimeError):
        verify_registration(registration.pk)
    registration.refresh_from_db()
    assert registration.last_verification_error == "timeout"
    assert registration.next_verification_at == AT + timedelta(minutes=5)


@pytest.mark.django_db
def test_delayed_closed_season_activity_preserves_historical_season(world) -> None:  # type: ignore[no-untyped-def]
    config, season, player = world
    creator, speaker = player(1801), player(1802)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1801,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=AT,
        last_seen_at=AT,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator,
        group=group,
        state=CreatorGroupRegistration.State.ACTIVE,
        reward_eligible_since=AT - timedelta(days=1),
    )
    Season.objects.filter(pk=season.pk).update(status=Season.Status.CLOSED)
    activity = record_activity(
        registration=registration,
        sender=speaker,
        message_id=1,
        update_id=1,
        text="historical delayed activity",
        observed_at=AT,
    )
    assert activity and activity.season_id == season.pk
    assert settle_completed_windows(now=AT + timedelta(minutes=26)) == 1
    outcome = CreatorWindowSettlement.objects.get(
        registration=registration, season=season, config_version=config
    )
    assert outcome.state == CreatorWindowSettlement.State.DEFERRED_SEASON_CLOSE


@pytest.mark.django_db
def test_event_time_creator_eligibility_fails_closed_before_activation_and_after_pause(
    world: Any,
) -> None:
    _, _, player = world
    creator, speaker = player(1811), player(1812)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1811,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=AT,
        last_seen_at=AT,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator,
        group=group,
        state=CreatorGroupRegistration.State.ACTIVE,
        reward_eligible_since=AT + timedelta(minutes=5),
    )
    before = record_activity(
        registration=registration,
        sender=speaker,
        message_id=1,
        update_id=1,
        text="observed before activation",
        observed_at=AT,
    )
    assert before and before.rewardable_chars == 0 and not before.program_active
    registration.state = CreatorGroupRegistration.State.ADMIN_REQUIRED
    registration.reward_eligible_since = None
    registration.save(update_fields=("state", "reward_eligible_since", "updated_at"))
    after = record_activity(
        registration=registration,
        sender=speaker,
        message_id=2,
        update_id=2,
        text="observed after pause state",
        observed_at=AT + timedelta(minutes=6),
    )
    assert after and after.rewardable_chars == 0 and not after.program_active


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("blocked_party", "state"),
    [
        ("inviter", Player.ModerationState.SUSPENDED),
        ("inviter", Player.ModerationState.BANNED),
        ("invitee", Player.ModerationState.SUSPENDED),
        ("invitee", Player.ModerationState.BANNED),
    ],
)
def test_referral_activation_blocks_moderated_party(world, blocked_party: str, state: str) -> None:  # type: ignore[no-untyped-def]
    config, _, player = world
    inviter, invitee = player(1821), player(1822)
    relation = attribute_start(invitee, get_or_create_code(inviter).code, at=AT)
    assert relation
    PlayerProgression.objects.create(
        player=invitee,
        lifetime_points=2500,
        level=5,
        dexo_stage="explorer",
        config_version=config,
    )
    blocked = inviter if blocked_party == "inviter" else invitee
    blocked.moderation_state = state
    blocked.save(update_fields=("moderation_state",))
    assert activate_relation(relation.pk, at=AT) is False
    assert RewardLedger.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("blocked_party", "state"),
    [
        ("inviter", Player.ModerationState.SUSPENDED),
        ("inviter", Player.ModerationState.BANNED),
        ("invitee", Player.ModerationState.SUSPENDED),
        ("invitee", Player.ModerationState.BANNED),
    ],
)
def test_passive_share_blocks_moderated_party_and_uses_source_config(
    world: Any, blocked_party: str, state: str
) -> None:
    config, season, player = world
    inviter, invitee = player(1831), player(1832)
    ReferralRelation.objects.create(
        inviter=inviter,
        invitee=invitee,
        status=ReferralRelation.Status.VERIFIED,
        attributed_at=AT - timedelta(days=2),
        verified_at=AT - timedelta(days=1),
        verified_config=config,
    )
    blocked = inviter if blocked_party == "inviter" else invitee
    blocked.moderation_state = state
    blocked.save(update_fields=("moderation_state",))
    source = RewardLedger.objects.create(
        player=invitee,
        season=season,
        source_type="DAILY",
        source_id=f"blocked-{blocked_party}-{state}",
        context="DAILY",
        gross_amount=100,
        player_amount=100,
        config_version=config,
        idempotency_key=f"blocked-{blocked_party}-{state}",
        occurred_at=AT,
    )
    outcome = process_source_reward(source)
    assert outcome.amount == 0 and outcome.share_reward is None
    assert outcome.source_reward.config_version_id == config.pk


@pytest.mark.django_db
def test_referral_api_tiers_are_derived_from_effective_config(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from django.http import HttpRequest

    from apps.referrals.api import referrals as referral_api

    _, _, player = world
    inviter = player(1841)
    for offset in range(5):
        ReferralRelation.objects.create(
            inviter=inviter,
            invitee=player(1850 + offset),
            status=ReferralRelation.Status.VERIFIED,
            attributed_at=AT,
            verified_at=AT,
        )
    monkeypatch.setattr("apps.referrals.services.settings.TELEGRAM_BOT_USERNAME", "dexo_bot")
    request = HttpRequest()
    request.auth = inviter  # type: ignore[attr-defined]
    payload = referral_api(request)
    assert payload["current_tier"] == "SILVER"
    assert payload["current_share_bps"] == 500
    assert payload["next_tier"] == {"key": "GOLD", "min_verified_referrals": 15, "share_bps": 700}


@pytest.mark.django_db
def test_network_activity_without_historical_season_fails_closed(world) -> None:  # type: ignore[no-untyped-def]
    _, season, player = world
    creator, speaker = player(1861), player(1862)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1861,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=AT,
        last_seen_at=AT,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator,
        group=group,
        state=CreatorGroupRegistration.State.ACTIVE,
        reward_eligible_since=AT - timedelta(days=1),
    )
    observed_at = season.ends_at + timedelta(seconds=1)
    activity = record_activity(
        registration=registration,
        sender=speaker,
        message_id=1,
        update_id=1,
        text="no historical season activity",
        observed_at=observed_at,
    )
    assert activity and activity.season is None
    assert not activity.program_active and activity.rewardable_chars == 0
