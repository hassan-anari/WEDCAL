# mypy: disable-error-code="no-untyped-def"
from datetime import UTC, datetime, timedelta

import pytest

from apps.arena.epochs import freeze_epoch, initialize_epochs
from apps.arena.models import EpochPerformanceSnapshot, PerformanceSourceFact
from apps.arena.qualification import record_qualification_decision
from apps.rewards.models import RewardSettlementOutbox
from apps.typing_game.models import TypingPhrase
from apps.typing_game.services import claim_wallet, start_attempt, submit_attempt

START = datetime(2026, 1, 1, tzinfo=UTC)
TEXT = "Dexo typing arena phrase"


def qualify(player) -> None:
    record_qualification_decision(
        player=player,
        policy_key="QUALIFIED_PLAYER",
        policy_version=1,
        decision="QUALIFIED",
        effective_at=START,
        reason_code="AUTHORITATIVE_TEST_REVIEW",
        source="TEST",
        source_id=f"typing-qualified:{player.pk}",
    )


def complete(player, key: str, at: datetime):
    attempt, _ = start_attempt(player, key, at)
    return submit_attempt(player, attempt.public_id, TEXT, at + timedelta(seconds=5))[0]


def core_snapshot(epoch, player) -> EpochPerformanceSnapshot:
    return EpochPerformanceSnapshot.objects.get(
        epoch=epoch, player=player, category="CORE_GAMEPLAY"
    )


@pytest.mark.django_db
def test_unclaimed_typing_credit_is_frozen_from_completion_source_time(
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config, starts_at=START)
    epoch = initialize_epochs(season)[0]
    player = player_factory()
    qualify(player)
    TypingPhrase.objects.create(key="arena", version=1, text=TEXT, enabled=True)

    attempt = complete(player, "epoch-one", START + timedelta(days=1))
    fact = PerformanceSourceFact.objects.get(source_type="TYPING_CREDIT")
    assert fact.source_id == str(attempt.public_id)
    assert fact.source_time == attempt.submitted_at
    assert fact.amount == attempt.reward_amount

    freeze_epoch(epoch, at=epoch.ends_at)
    raw = core_snapshot(epoch, player).raw_inputs
    assert raw["durable_sources"]["TYPING_CREDIT"] == {
        "amount": attempt.reward_amount,
        "events": 1,
    }
    assert raw["durable_source_amount"] == attempt.reward_amount
    assert raw["player_amount"] == 0


@pytest.mark.django_db
def test_claim_before_freeze_and_unclaimed_source_sets_have_identical_raw_performance(
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config, starts_at=START)
    epoch = initialize_epochs(season)[0]
    claimed, unclaimed = player_factory(92001), player_factory(92002)
    qualify(claimed)
    qualify(unclaimed)
    TypingPhrase.objects.create(key="arena", version=1, text=TEXT, enabled=True)
    at = START + timedelta(days=1)
    complete(claimed, "claimed", at)
    complete(unclaimed, "unclaimed", at)
    claim_wallet(claimed, "claim-before-freeze", at + timedelta(hours=1))
    RewardSettlementOutbox.objects.update(state="PROCESSED")

    freeze_epoch(epoch, at=epoch.ends_at)
    claimed_raw = core_snapshot(epoch, claimed).raw_inputs
    unclaimed_raw = core_snapshot(epoch, unclaimed).raw_inputs
    assert claimed_raw == unclaimed_raw
    assert claimed_raw["reward_event_count"] == 0
    assert claimed_raw["durable_source_events"] == 1


@pytest.mark.django_db
def test_typing_credits_spanning_epochs_remain_at_source_time_when_claimed_together(
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config, starts_at=START)
    first, second = initialize_epochs(season)[:2]
    player = player_factory()
    qualify(player)
    TypingPhrase.objects.create(key="arena", version=1, text=TEXT, enabled=True)

    first_attempt = complete(player, "first", START + timedelta(days=1))
    freeze_epoch(first, at=first.ends_at)
    first_raw_before_claim = core_snapshot(first, player).raw_inputs

    second_attempt = complete(player, "second", START + timedelta(days=8))
    claim_wallet(player, "combined-claim", START + timedelta(days=8, hours=1))
    freeze_epoch(second, at=second.ends_at)

    assert core_snapshot(first, player).raw_inputs == first_raw_before_claim
    assert first_raw_before_claim["durable_source_amount"] == first_attempt.reward_amount
    assert core_snapshot(second, player).raw_inputs["durable_source_amount"] == (
        second_attempt.reward_amount
    )
    assert PerformanceSourceFact.objects.filter(source_type="TYPING_CREDIT").count() == 2


@pytest.mark.django_db
def test_typing_credit_retry_cannot_duplicate_performance_fact(
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season_factory(config, starts_at=START)
    player = player_factory()
    TypingPhrase.objects.create(key="arena", version=1, text=TEXT, enabled=True)
    attempt, _ = start_attempt(player, "retry", START + timedelta(days=1))

    first, created = submit_attempt(
        player, attempt.public_id, TEXT, START + timedelta(days=1, seconds=5)
    )
    retry, retry_created = submit_attempt(
        player, attempt.public_id, TEXT, START + timedelta(days=1, seconds=6)
    )
    assert created and not retry_created and retry.pk == first.pk
    assert (
        PerformanceSourceFact.objects.filter(
            source_type="TYPING_CREDIT", source_id=str(attempt.public_id)
        ).count()
        == 1
    )
