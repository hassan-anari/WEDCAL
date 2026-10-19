# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from datetime import timedelta

import pytest

from apps.arena.economics import prepare_epoch
from apps.arena.epochs import drain_blockers, freeze_epoch
from apps.arena.models import (
    EconomicDeferral,
    EconomicRecognition,
    EpochPerformanceSnapshot,
    PerformanceSourceFact,
    QualificationDecision,
)
from apps.arena.readiness import evaluate_epoch_readiness
from apps.daily.services import claim_daily
from apps.missions.models import MissionDefinition
from apps.missions.services import MissionUnavailable, claim_mission, start_mission, verify_mission
from apps.network.models import CreatorGroupRegistration, CreatorRegistrationStateEvent
from apps.network.services import record_activity, settle_window
from apps.referrals.models import ReferralRelation
from apps.referrals.services import activate_relation
from apps.rewards.services import settle_player_reward
from apps.telegram.models import TelegramGroup, TelegramInboundUpdate
from apps.typing_game.services import claim_wallet

from .conftest import START

pytestmark = pytest.mark.django_db


def two_days(world, player, epoch=0):
    for day in (1, 2):
        world.typing(player, world.epochs[epoch].starts_at + timedelta(days=day))


def snapshots(epoch, player):
    return {
        r.category: r for r in EpochPerformanceSnapshot.objects.filter(epoch=epoch, player=player)
    }


def mission(world):
    world.clock["at"] = START
    return MissionDefinition.objects.create(
        key="visit-dalexio",
        version=1,
        enabled=True,
        verification_family="TIMED_EXTERNAL",
        repeat_policy="ONCE_PER_DEFINITION_VERSION",
        verification_parameters={"destination_url": "https://dalexio.com/"},
    )


def registration(world, owner, identity=-93001):
    group = TelegramGroup.objects.create(
        telegram_chat_id=identity,
        chat_type="supergroup",
        first_seen_at=START,
        last_seen_at=START,
        bot_is_admin=True,
    )
    row = CreatorGroupRegistration.objects.create(
        creator=owner,
        group=group,
        state="ACTIVE",
        reward_eligible_since=START,
        bot_verified_at=START,
        creator_verified_at=START,
    )
    CreatorRegistrationStateEvent.objects.create(
        registration=row,
        state="ACTIVE",
        reward_eligible=True,
        occurred_at=START,
        source="SYNTHETIC_VERIFIED",
        owner=owner,
    )
    return row


def creator_day(world, reg, speakers, day, late=False):
    for slot in range(4):
        at = START + timedelta(days=day, hours=slot)
        for index, speaker in enumerate(speakers):
            update = day * 100000 + slot * 1000 + speaker.pk * 10 + abs(reg.pk)
            world.clock["at"] = at + timedelta(seconds=index)
            TelegramInboundUpdate.objects.create(
                update_id=update,
                payload={"update_id": update},
                state="PROCESSED",
                webhook_authenticated_at=world.clock["at"],
                payload_expires_at=at + timedelta(days=7),
            )
            record_activity(
                registration=reg,
                sender=speaker,
                message_id=update,
                update_id=update,
                text=(
                    f"owner {reg.pk} speaker {speaker.pk} day {day} slot {slot} "
                    + "meaningful topic " * 20
                )[:200],
                observed_at=world.clock["at"],
            )
        from apps.core.activity import window_start_for

        start = window_start_for(at, 900)
        world.clock["at"] = (
            world.epochs[1].starts_at + timedelta(minutes=15 + slot)
            if late
            else at + timedelta(minutes=16)
        )
        settle_window(reg.pk, start, world.config.pk, world.season.pk)


def test_verified_unclaimed_mission_and_later_claim_are_epoch_one_only(economic_world):
    w = economic_world
    p = w.player()
    mission(w)
    two_days(w, p)
    at = START + timedelta(days=3)
    w.clock["at"] = at
    opened = start_mission(p, "visit-dalexio", at)
    w.clock["at"] = at + timedelta(seconds=6)
    progress = verify_mission(p, "visit-dalexio", w.clock["at"], opened.open_token)
    assert progress.status == "REWARD_READY" and progress.reward_ledger_id is None
    fact = PerformanceSourceFact.objects.get(source_type="PERSONAL_MISSION_VERIFIED")
    assert fact.source_time == progress.verified_at
    assert fact.metadata["verification_scope"] == "SERVER_STRATEGY_NOT_READING_OR_HUMAN_IDENTITY"
    w.freeze(w.epochs[0])
    before = snapshots(w.epochs[0], p)["CORE_GAMEPLAY"].raw_inputs
    assert before["components"]["mission"] == 125
    w.clock["at"] = START + timedelta(days=8)
    claimed, created = claim_mission(p, "visit-dalexio", w.clock["at"])
    assert claimed.reward_ledger is not None
    assert created and claimed.reward_ledger.occurred_at == fact.source_time
    claim_mission(p, "visit-dalexio", w.clock["at"])
    two_days(w, p, 1)
    w.freeze(w.epochs[1])
    assert snapshots(w.epochs[0], p)["CORE_GAMEPLAY"].raw_inputs == before
    assert snapshots(w.epochs[1], p)["CORE_GAMEPLAY"].raw_inputs["components"]["mission"] == 0
    assert (
        PerformanceSourceFact.objects.filter(source_type="PERSONAL_MISSION_VERIFIED").count() == 1
    )


def test_no_activated_missions_and_incompatible_brief_do_not_create_performance(economic_world):
    w = economic_world
    p = w.player()
    two_days(w, p)
    with pytest.raises(MissionUnavailable):
        start_mission(p, "read-season-brief", START + timedelta(days=3))
    MissionDefinition.objects.create(
        key="read-season-brief",
        version=1,
        enabled=True,
        verification_family="INTERNAL",
        repeat_policy="ONCE_PER_DEFINITION_VERSION",
    )
    with pytest.raises(MissionUnavailable):
        verify_mission(p, "read-season-brief", START + timedelta(days=3))
    w.freeze(w.epochs[0])
    r = snapshots(w.epochs[0], p)
    assert r["CORE_GAMEPLAY"].raw_inputs["components"]["mission"] == 0
    assert r["CREATOR_COMMUNITY_GROWTH"].performance_units == 0


def test_typing_claim_and_arbitrary_xp_sources_cannot_inflate_core(economic_world):
    w = economic_world
    p = w.player()
    two_days(w, p)
    w.clock["at"] = START + timedelta(days=3)
    before = list(
        EconomicRecognition.objects.filter(epoch=w.epochs[0], player=p).values_list(
            "evidence_digest", flat=True
        )
    )
    claim_wallet(p, "p3-claim", w.clock["at"])
    for source in [
        "CLAN_ACTIVITY",
        "CLAN_MISSION",
        "CREATOR",
        "REFERRAL",
        "REFERRAL_SHARE",
        "CAPTAIN_BONUS",
        "ADMIN_ADJUSTMENT",
        "MISSION",
        "NETWORK_SPEAKER",
    ]:
        settle_player_reward(
            player=p,
            source_type=source,
            source_id=f"xp-only:{source}",
            context="SYNTHETIC",
            amount=10000,
            idempotency_key=f"p3:{source}",
            occurred_at=w.clock["at"],
            resolved_config=w.resolved,
        )
    assert (
        list(
            EconomicRecognition.objects.filter(epoch=w.epochs[0], player=p).values_list(
                "evidence_digest", flat=True
            )
        )
        == before
    )
    w.freeze(w.epochs[0])
    r = snapshots(w.epochs[0], p)
    assert r["CORE_GAMEPLAY"].raw_inputs["components"]["mission"] == 0
    assert r["CORE_GAMEPLAY"].raw_inputs["components"]["typing"] > 0
    assert (
        r["CLAN_PERFORMANCE"].performance_units
        == r["CREATOR_COMMUNITY_GROWTH"].performance_units
        == r["VERIFIED_REFERRAL"].performance_units
        == 0
    )
    assert r["LOYALTY_CONSISTENCY"].raw_inputs["meaningful_days"] == [1, 2]


def test_eight_utc_daily_claims_still_score_at_most_one_thousand(economic_world):
    w = economic_world
    p = w.player()
    two_days(w, p)
    for delta in [timedelta(minutes=1)] + [
        timedelta(days=i, hours=1, minutes=30) for i in range(7)
    ]:
        at = START + delta
        w.clock["at"] = at
        claim_daily(p, at)
    w.freeze(w.epochs[0])
    raw = snapshots(w.epochs[0], p)["CORE_GAMEPLAY"].raw_inputs
    assert len(raw["daily_periods"]) == 8 and raw["components"]["daily"] == 1000


def test_referral_level_five_actual_week_two_activation_without_epoch_dependency(
    economic_world, monkeypatch
):
    w = economic_world
    alice, bob = w.player(), w.player()
    two_days(w, alice)
    two_days(w, bob)
    relation = ReferralRelation.objects.create(inviter=alice, invitee=bob, attributed_at=START)
    w.clock["at"] = START + timedelta(days=3)
    settle_player_reward(
        player=bob,
        source_type="ADMIN_ADJUSTMENT",
        source_id="level3",
        context="SYNTHETIC",
        amount=500,
        idempotency_key="p3-level3",
        occurred_at=w.clock["at"],
        resolved_config=w.resolved,
    )
    assert not activate_relation(relation.pk, at=w.clock["at"])
    assert not QualificationDecision.objects.exists()
    w.clock["at"] = START + timedelta(days=8)
    settle_player_reward(
        player=bob,
        source_type="ADMIN_ADJUSTMENT",
        source_id="level5",
        context="SYNTHETIC",
        amount=2500,
        idempotency_key="p3-level5",
        occurred_at=w.clock["at"],
        resolved_config=w.resolved,
    )
    monkeypatch.setattr(
        "apps.arena.qualification.is_player_qualified",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("circular qualification dependency")),
    )
    assert activate_relation(relation.pk, at=w.clock["at"])
    assert not activate_relation(relation.pk, at=w.clock["at"])
    relation.refresh_from_db()
    assert relation.verified_at == START + timedelta(days=8)
    assert not QualificationDecision.objects.exists()
    w.freeze(w.epochs[0])
    assert snapshots(w.epochs[0], alice)["VERIFIED_REFERRAL"].performance_units == 0
    two_days(w, alice, 1)
    w.freeze(w.epochs[1])
    assert snapshots(w.epochs[1], alice)["VERIFIED_REFERRAL"].performance_units == 1666
    assert EconomicRecognition.objects.filter(source_type="REFERRAL_ACTIVATION").count() == 1


def test_pending_referral_does_not_block_unrelated_freeze(economic_world):
    w = economic_world
    alice, bob = w.player(), w.player()
    two_days(w, alice)
    relation = ReferralRelation.objects.create(inviter=alice, invitee=bob, attributed_at=START)
    assert not activate_relation(relation.pk, at=START + timedelta(days=3))
    assert drain_blockers(w.epochs[0]) == []
    w.freeze(w.epochs[0])
    assert snapshots(w.epochs[0], alice)["VERIFIED_REFERRAL"].performance_units == 0
    assert not EconomicDeferral.objects.filter(provider="REFERRAL_ACTIVATION").exists()


def test_creator_owner_speaker_and_multiple_category_sources_are_isolated(economic_world):
    w = economic_world
    owner = w.player()
    speakers = [w.player() for _ in range(3)]
    two_days(w, owner)
    for p in speakers:
        w.typing(p, START + timedelta(hours=1))
    reg = registration(w, owner)
    creator_day(w, reg, speakers, 1)
    creator_day(w, reg, speakers, 2)
    w.freeze(w.epochs[0])
    r = snapshots(w.epochs[0], owner)
    assert r["CREATOR_COMMUNITY_GROWTH"].performance_units > 0
    assert r["CREATOR_COMMUNITY_GROWTH"].raw_inputs["h_units"] == 60000
    assert r["CREATOR_COMMUNITY_GROWTH"].raw_inputs["windows"] == [0, 4, 4, 0, 0, 0, 0]
    assert r["CORE_GAMEPLAY"].raw_inputs["components"]["typing"] > 0
    assert all(key in {"TYPING_CREDIT"} for key in r["CORE_GAMEPLAY"].raw_inputs["durable_sources"])
    assert r["VERIFIED_REFERRAL"].performance_units == 0
    assert r["LOYALTY_CONSISTENCY"].performance_units > 0
    from apps.arena.corrections import corrected_category_scores
    from apps.network.services import purge_activity_evidence

    before = (
        r["CREATOR_COMMUNITY_GROWTH"].performance_units,
        r["CREATOR_COMMUNITY_GROWTH"].raw_inputs,
    )
    purge_activity_evidence(now=START + timedelta(days=91))
    assert corrected_category_scores(w.epochs[0])[owner.pk]["CREATOR_COMMUNITY_GROWTH"] == before


def test_late_telegram_finalization_recognizes_week_two_once(economic_world):
    w = economic_world
    owner = w.player()
    speakers = [w.player() for _ in range(3)]
    for p in [owner, *speakers]:
        two_days(w, p)
    reg = registration(w, owner)
    # Persist source work without finalizing its window before week one closes.
    at = w.epochs[0].ends_at - timedelta(minutes=10)
    for i, p in enumerate(speakers):
        w.clock["at"] = at
        uid = 88100 + i
        TelegramInboundUpdate.objects.create(
            update_id=uid,
            payload={},
            state="PROCESSED",
            webhook_authenticated_at=at,
            payload_expires_at=at + timedelta(days=7),
        )
        record_activity(
            registration=reg,
            sender=p,
            message_id=uid,
            update_id=uid,
            text=f"speaker {i} " + "different topic " * 20,
            observed_at=at,
        )
    w.freeze(w.epochs[0])
    assert snapshots(w.epochs[0], owner)["CREATOR_COMMUNITY_GROWTH"].performance_units == 0
    assert (
        EconomicDeferral.objects.filter(epoch=w.epochs[0], provider="NETWORK_ACTIVITY").count() == 3
    )
    from apps.core.activity import window_start_for

    w.clock["at"] = w.epochs[1].starts_at + timedelta(minutes=20)
    window = settle_window(reg.pk, window_start_for(at, 900), w.config.pk, w.season.pk)
    settle_window(reg.pk, window.window_start, w.config.pk, w.season.pk)
    records = EconomicRecognition.objects.filter(source_type="CREATOR_ACTIVITY")
    assert records.count() == 3 and set(records.values_list("epoch_id", flat=True)) == {
        w.epochs[1].pk
    }
    assert all(r.source_time == at and r.recognized_at == w.clock["at"] for r in records)


def test_individual_flag_is_deferred_while_systemic_unknown_ingress_blocks(economic_world):
    w = economic_world
    good, review = w.player(), w.player()
    two_days(w, good)
    two_days(w, review)
    review.moderation_state = "FLAGGED"
    review.save(update_fields=["moderation_state"])
    w.freeze(w.epochs[0])
    assert snapshots(w.epochs[0], good) and not snapshots(w.epochs[0], review)
    assert QualificationDecision.objects.filter(player=review, decision="PENDING").exists()
    assert EconomicDeferral.objects.filter(player=review, provider="PLAYER_REVIEW").exists()
    w.clock["at"] = w.epochs[1].ends_at
    TelegramInboundUpdate.objects.create(
        update_id=999999,
        payload={"unknown": True},
        state="DEAD_LETTER",
        payload_expires_at=w.clock["at"] + timedelta(days=1),
    )
    prepare_epoch(w.epochs[1])
    run = evaluate_epoch_readiness(w.epochs[1], evaluation_key="systemic")
    assert run.status == "BLOCKED" and "UNSCOPED_TELEGRAM_INGRESS" in str(run.blocking_reasons)


def test_pending_network_arrival_after_prepare_invalidates_manifest(economic_world):
    w = economic_world
    owner = w.player()
    speaker = w.player()
    two_days(w, owner)
    reg = registration(w, owner)
    w.clock["at"] = w.epochs[0].ends_at
    prepare_epoch(w.epochs[0])
    run = evaluate_epoch_readiness(w.epochs[0], evaluation_key="before-arrival")
    assert run.status == "COMPLETE"
    record_activity(
        registration=reg,
        sender=speaker,
        message_id=991,
        update_id=991,
        text="A delayed authoritative group message with sufficient length",
        observed_at=w.epochs[0].ends_at - timedelta(minutes=3),
    )
    from apps.arena.epochs import EpochBlocked

    with pytest.raises(EpochBlocked, match="STALE"):
        freeze_epoch(w.epochs[0], at=w.clock["at"])
    w.freeze(w.epochs[0])
    assert EconomicDeferral.objects.filter(provider="NETWORK_ACTIVITY").exists()


def test_proven_future_ingress_does_not_invalidate_prior_manifest(economic_world):
    w = economic_world
    p = w.player()
    two_days(w, p)
    w.clock["at"] = w.epochs[0].ends_at
    prepare_epoch(w.epochs[0])
    run = evaluate_epoch_readiness(w.epochs[0], evaluation_key="before-future-message")
    assert run.status == "COMPLETE"
    TelegramInboundUpdate.objects.create(
        update_id=878787,
        payload={
            "message": {
                "date": int((w.epochs[1].starts_at + timedelta(minutes=1)).timestamp()),
                "from": {"id": p.telegram_user_id},
            }
        },
        state="PENDING",
        webhook_authenticated_at=w.clock["at"],
        payload_expires_at=w.clock["at"] + timedelta(days=7),
    )
    assert freeze_epoch(w.epochs[0], at=w.clock["at"]).state == "FROZEN"


def test_cleared_frozen_review_allows_referral_activation_without_future_epoch(economic_world):
    from apps.arena.economics import review_player
    from apps.arena.qualification import record_qualification_decision

    w = economic_world
    alice, bob = w.player(), w.player()
    two_days(w, alice)
    two_days(w, bob)
    record_qualification_decision(
        player=bob,
        policy_key="QUALIFIED_PLAYER",
        policy_version=1,
        decision="PENDING",
        effective_at=START,
        reason_code="REVIEW",
        source="SYNTHETIC_REVIEW",
        source_id="pending",
    )
    relation = ReferralRelation.objects.create(inviter=alice, invitee=bob, attributed_at=START)
    w.freeze(w.epochs[0])
    w.clock["at"] = START + timedelta(days=8)
    review_player(
        w.epochs[0],
        bob,
        outcome="CLEARED",
        actor=w.actor,
        reason="legitimate participation",
        evidence_reference="reviewed evidence",
    )
    settle_player_reward(
        player=bob,
        source_type="ADMIN_ADJUSTMENT",
        source_id="cleared-level5",
        context="SYNTHETIC",
        amount=3000,
        idempotency_key="cleared-level5",
        occurred_at=w.clock["at"],
        resolved_config=w.resolved,
    )
    assert activate_relation(relation.pk, at=w.clock["at"])
    assert not QualificationDecision.objects.filter(evidence__epoch_id=w.epochs[1].pk).exists()


@pytest.mark.parametrize("economic_world", ["v2"], indirect=True)
def test_real_tap_v2_batch_is_allowlisted_once_and_later_claims_do_not_add_taps(
    economic_world, settings
):
    from uuid import uuid4

    from apps.base_tap.schemas import BatchInput
    from apps.base_tap.v2 import issue_lease, submit_batch

    w = economic_world
    p = w.player()
    two_days(w, p)
    settings.TAP_V2_ISSUANCE_ENABLED = True
    settings.BASE_TAP_LEGACY_INGRESS_ENABLED = False
    at = START + timedelta(days=3)
    w.clock["at"] = at
    state = issue_lease(p, session=uuid4(), at=at)
    lease = state["lease"]
    batch = BatchInput(
        lease_id=lease["lease_id"],
        session_id=lease["session_id"],
        generation=lease["generation"],
        sequence_start=1,
        tap_count=10,
        idempotency_key="v2-economic-source",
    )
    first = submit_batch(p, batch, at=at)
    assert first["accepted_taps"] == 10
    submit_batch(p, batch, at=at + timedelta(seconds=1))
    assert EconomicRecognition.objects.filter(source_type="BASE_TAP_V2_BATCH").count() == 1
    w.freeze(w.epochs[0])
    raw = snapshots(w.epochs[0], p)["CORE_GAMEPLAY"].raw_inputs
    assert sum(raw["tap_by_day"]) == 10 and raw["components"]["tap"] > 0
    assert set(raw["durable_sources"]) == {"TYPING_CREDIT", "BASE_TAP_V2_BATCH"}


def test_community_only_route_does_not_manufacture_creator_breadth(economic_world):
    w = economic_world
    owner = w.player()
    speakers = [w.player() for _ in range(3)]
    two_days(w, owner)
    reg = registration(w, owner)
    creator_day(w, reg, speakers, 1)
    creator_day(w, reg, speakers, 2)
    w.freeze(w.epochs[0])
    assert snapshots(w.epochs[0], owner)["CREATOR_COMMUNITY_GROWTH"].performance_units == 0
    for speaker in speakers:
        rows = snapshots(w.epochs[0], speaker)
        assert rows["CORE_GAMEPLAY"].performance_units == 0
        assert rows["LOYALTY_CONSISTENCY"].performance_units == 2857
