# mypy: disable-error-code="no-untyped-def"
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from apps.arena.epochs import freeze_epoch, initialize_epochs
from apps.arena.models import EpochPerformanceSnapshot, PerformanceSourceFact
from apps.arena.qualification import record_qualification_decision
from apps.referrals.models import ReferralRelation
from apps.referrals.services import process_source_reward
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
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
        source_id=f"typing-referral-qualified:{player.pk}",
    )


def relation(inviter, invitee, config, verified_at=START) -> ReferralRelation:
    return ReferralRelation.objects.create(
        inviter=inviter,
        invitee=invitee,
        status=ReferralRelation.Status.VERIFIED,
        attributed_at=verified_at,
        verified_at=verified_at,
        verified_config=config,
    )


def complete(player, key: str, at: datetime):
    attempt, _ = start_attempt(player, key, at)
    return submit_attempt(player, attempt.public_id, TEXT, at + timedelta(seconds=5))[0]


def referral_raw(epoch, inviter) -> dict[str, object]:
    return cast(
        dict[str, object],
        EpochPerformanceSnapshot.objects.get(
            epoch=epoch, player=inviter, category="VERIFIED_REFERRAL"
        ).raw_inputs,
    )


def settle_claim_referral(invitee, key: str, at: datetime) -> RewardLedger:
    claim_wallet(invitee, key, at)
    source = RewardLedger.objects.get(player=invitee, source_type="TYPING_CLAIM")
    outcome = process_source_reward(source)
    assert outcome.share_reward_id is not None
    RewardSettlementOutbox.objects.update(state=RewardSettlementOutbox.State.PROCESSED)
    return cast(RewardLedger, outcome.share_reward)


@pytest.mark.django_db
def test_unclaimed_typing_credit_freezes_inviter_referral_source_time(
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config, starts_at=START)
    epoch = initialize_epochs(season)[0]
    inviter, invitee = player_factory(93001), player_factory(93002)
    qualify(inviter)
    relation(inviter, invitee, config)
    TypingPhrase.objects.create(key="arena", version=1, text=TEXT, enabled=True)

    attempt = complete(invitee, "unclaimed-referral", START + timedelta(days=1))
    fact = PerformanceSourceFact.objects.get(source_type="TYPING_REFERRAL_CREDIT")
    assert fact.player == inviter
    assert fact.source_time == attempt.submitted_at
    assert fact.metadata == {
        "invitee_id": invitee.pk,
        "relation_id": invitee.referral_relation.pk,
        "tier_key": "BRONZE",
        "share_bps": 300,
        "typing_credit_amount": attempt.reward_amount,
    }

    freeze_epoch(epoch, at=epoch.ends_at)
    assert referral_raw(epoch, inviter) == {
        "durable_source_amount": fact.amount,
        "durable_source_events": 1,
        "durable_source_type": "TYPING_REFERRAL_CREDIT",
    }


@pytest.mark.django_db
def test_claimed_and_unclaimed_typing_sources_freeze_identical_referral_inputs(
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config, starts_at=START)
    epoch = initialize_epochs(season)[0]
    inviter_a, invitee_a = player_factory(93101), player_factory(93102)
    inviter_b, invitee_b = player_factory(93103), player_factory(93104)
    for inviter, invitee in ((inviter_a, invitee_a), (inviter_b, invitee_b)):
        qualify(inviter)
        relation(inviter, invitee, config)
    TypingPhrase.objects.create(key="arena", version=1, text=TEXT, enabled=True)
    source_time = START + timedelta(days=1)
    complete(invitee_a, "claimed", source_time)
    complete(invitee_b, "unclaimed", source_time)
    settle_claim_referral(invitee_a, "before-freeze", source_time + timedelta(hours=1))

    freeze_epoch(epoch, at=epoch.ends_at)
    assert referral_raw(epoch, inviter_a) == referral_raw(epoch, inviter_b)
    assert referral_raw(epoch, inviter_a)["durable_source_events"] == 1


@pytest.mark.django_db
def test_combined_cross_epoch_claim_preserves_each_referral_source_epoch(
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config, starts_at=START)
    first, second = initialize_epochs(season)[:2]
    inviter, invitee = player_factory(93201), player_factory(93202)
    qualify(inviter)
    relation(inviter, invitee, config)
    TypingPhrase.objects.create(key="arena", version=1, text=TEXT, enabled=True)

    first_attempt = complete(invitee, "epoch-one", START + timedelta(days=1))
    freeze_epoch(first, at=first.ends_at)
    first_before_claim = referral_raw(first, inviter)
    second_attempt = complete(invitee, "epoch-two", START + timedelta(days=8))
    settle_claim_referral(invitee, "combined", START + timedelta(days=8, hours=1))
    freeze_epoch(second, at=second.ends_at)

    assert referral_raw(first, inviter) == first_before_claim
    assert first_before_claim["durable_source_amount"] == first_attempt.reward_amount * 300 // 10000
    assert referral_raw(second, inviter)["durable_source_amount"] == (
        second_attempt.reward_amount * 300 // 10000
    )
    assert PerformanceSourceFact.objects.filter(source_type="TYPING_REFERRAL_CREDIT").count() == 2


@pytest.mark.django_db
def test_later_referral_ledger_does_not_double_count_or_change_source_tier(
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config, starts_at=START)
    epoch = initialize_epochs(season)[0]
    inviter, invitee = player_factory(93301), player_factory(93302)
    qualify(inviter)
    relation(inviter, invitee, config)
    TypingPhrase.objects.create(key="arena", version=1, text=TEXT, enabled=True)
    attempt = complete(invitee, "retry", START + timedelta(days=1))
    retry, created = submit_attempt(
        invitee, attempt.public_id, TEXT, START + timedelta(days=1, seconds=6)
    )
    assert not created and retry.pk == attempt.pk
    fact = PerformanceSourceFact.objects.get(source_type="TYPING_REFERRAL_CREDIT")
    assert fact.metadata["tier_key"] == "BRONZE"
    # Later verified referrals would make the inviter SILVER at claim time, but cannot
    # reinterpret the already persisted source-time tier for this Typing credit.
    for index in range(4):
        later_invitee = player_factory(93400 + index)
        relation(
            inviter,
            later_invitee,
            config,
            verified_at=START + timedelta(days=2, seconds=index),
        )
    assert PerformanceSourceFact.objects.filter(source_type="TYPING_REFERRAL_CREDIT").count() == 1

    freeze_epoch(epoch, at=epoch.ends_at)
    frozen = referral_raw(epoch, inviter)
    share_reward = settle_claim_referral(invitee, "late", START + timedelta(days=8))
    assert share_reward.source_type == "REFERRAL"
    assert referral_raw(epoch, inviter) == frozen
    assert frozen["durable_source_events"] == 1
    assert frozen.get("events", 0) == 0
    fact.refresh_from_db()
    assert fact.metadata["tier_key"] == "BRONZE"
