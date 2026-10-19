# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.arena.corrections import (
    assess_correction,
    finalize_correction_round,
    pay_correction_round,
    record_correction_adjustment,
)
from apps.arena.economics import prepare_epoch
from apps.arena.epochs import freeze_epoch, player_dlx_balance, reconcile_epoch, settle_epoch
from apps.arena.models import (
    CategoryCorrectionReserve,
    DlxAllocation,
    DlxCorrection,
    EconomicDeferral,
    EconomicRecognition,
    EpochCategoryBudget,
)
from apps.arena.policy import CATEGORIES
from apps.arena.readiness import evaluate_epoch_readiness
from apps.arena.recognition import defer_source, record_recognition
from apps.clans.economy import activate_item, settle_clan_reward
from apps.clans.missions import record_mission_progress
from apps.clans.models import (
    Clan,
    ClanContributionEntry,
    ClanInventory,
    ClanMembership,
    ClanMembershipEvent,
    ClanMissionDefinition,
    ClanSeason,
)
from apps.daily.services import claim_daily
from apps.missions.services import start_mission, verify_mission
from apps.referrals.models import ReferralRelation
from apps.referrals.services import activate_relation
from apps.rewards.services import settle_player_reward
from apps.seasons.close import reconcile_season_dlx
from apps.telegram.models import TelegramGroup

from .conftest import START
from .test_lifecycle_isolation import creator_day, mission, registration, snapshots, two_days


def clan_member(w, p):
    w.clock["at"] = START
    group = TelegramGroup.objects.create(
        telegram_chat_id=-900000 - p.pk,
        chat_type="supergroup",
        first_seen_at=START,
        last_seen_at=START,
    )
    identity = Clan.objects.create(
        telegram_group=group,
        name=f"Clan {p.pk}",
        tag=f"p{p.pk}",
        normalized_tag=f"p{p.pk}",
        creator=p,
    )
    cs = ClanSeason.objects.create(
        clan=identity, season=w.season, captain=p, base_capacity=20, config_version=w.config
    )
    member = ClanMembership.objects.create(
        player=p,
        season=w.season,
        clan_season=cs,
        role="CAPTAIN",
        started_at=START,
        config_version=w.config,
    )
    ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=START)
    return member


def contribution(w, member, at, key):
    w.clock["at"] = at
    return settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id=key,
        gross_amount=100,
        idempotency_key=key,
        occurred_at=at,
        resolved_config=w.resolved,
    )[0]


def settle_first(w):
    w.freeze(w.epochs[0])
    w.clock["at"] = START + timedelta(days=14)
    return settle_epoch(w.epochs[0], at=w.clock["at"])


def correction_case(w, p, identity="omitted"):
    two_days(w, p)
    existing = EconomicDeferral.objects.filter(
        epoch=w.epochs[0], player=p, provider="TYPING_CREDIT"
    ).first()
    if existing:
        return existing
    return defer_source(
        w.epochs[0],
        "TYPING_CREDIT",
        f"{identity}:{p.pk}",
        reason="FUNDED_CORRECTION_REQUIRED",
        player_id=p.pk,
        category=CATEGORIES[1],
        evidence={"synthetic_verified_receipt": identity},
    )


@pytest.mark.django_db
def test_same_player_all_five_categories_canonical_booster_and_clan_mission(
    economic_world, settings
):
    w = economic_world
    owner = w.player()
    speakers = [w.player() for _ in range(3)]
    member = clan_member(w, owner)
    two_days(w, owner)
    for p in speakers:
        two_days(w, p)
    baseline = contribution(w, member, START + timedelta(days=1, hours=2), "unboosted")
    ClanInventory.objects.create(
        clan_season=member.clan_season, item_id="contribution-booster", quantity=1
    )
    at = START + timedelta(days=2, hours=2)
    w.clock["at"] = at
    activate_item(
        clan_season=member.clan_season,
        actor=owner,
        item_id="contribution-booster",
        idempotency_key="boost",
        resolved_config=w.resolved,
        at=at,
    )
    boosted = contribution(w, member, at + timedelta(minutes=1), "boosted")
    assert baseline.contribution_amount == 100 and boosted.contribution_amount == 115
    definition = ClanMissionDefinition.objects.create(
        key="p3-individual",
        version=1,
        kind="INDIVIDUAL",
        requirement="TEST",
        target_units=1,
        reward_band="SMALL",
        starts_at=at,
        ends_at=at + timedelta(hours=1),
        enabled=True,
        config_version=w.config,
    )
    w.clock["at"] = at + timedelta(minutes=2)
    progress = record_mission_progress(
        definition=definition,
        membership=member,
        source_id="independently-verified",
        units=1,
        occurred_at=w.clock["at"],
        resolved_config=w.resolved,
    )
    assert progress.completed_at
    mission(w)
    at = START + timedelta(days=3)
    w.clock["at"] = at
    opened = start_mission(owner, "visit-dalexio", at)
    w.clock["at"] = at + timedelta(seconds=6)
    verify_mission(owner, "visit-dalexio", w.clock["at"], opened.open_token)
    claim_daily(owner, w.clock["at"])
    from apps.base_tap.services import submit_tap_batch

    settings.BASE_TAP_LEGACY_INGRESS_ENABLED = True
    submit_tap_batch(owner, tap_count=10, idempotency_key="mixed-legacy-tap", at=w.clock["at"])
    relation = ReferralRelation.objects.create(
        inviter=owner, invitee=speakers[0], attributed_at=START
    )
    settle_player_reward(
        player=speakers[0],
        source_type="ADMIN_ADJUSTMENT",
        source_id="level-five",
        context="SYNTHETIC",
        amount=3000,
        idempotency_key="level-five",
        occurred_at=w.clock["at"],
        resolved_config=w.resolved,
    )
    assert activate_relation(relation.pk, at=w.clock["at"])
    reg = registration(w, owner)
    creator_day(w, reg, speakers, 1)
    creator_day(w, reg, speakers, 2)
    w.freeze(w.epochs[0])
    rows = snapshots(w.epochs[0], owner)
    assert all(rows[c].performance_units > 0 for c in CATEGORIES)
    canonical = list(ClanContributionEntry.objects.filter(membership=member))
    assert len(canonical) == 3
    assert sum(rows[CATEGORIES[0]].raw_inputs["contribution_by_day"]) == sum(
        e.amount for e in canonical
    )
    assert EconomicRecognition.objects.filter(source_type="CLAN_CONTRIBUTION").count() == 3
    core = rows[CATEGORIES[1]].raw_inputs
    assert core["components"]["mission"] == 125 and core["components"]["daily"] == 142
    assert set(core["durable_sources"]) == {
        "TYPING_CREDIT",
        "PERSONAL_MISSION_VERIFIED",
        "BASE_TAP_BATCH",
    }
    assert rows[CATEGORIES[3]].performance_units == 1666


@pytest.mark.django_db
def test_two_player_emission_empty_categories_and_six_epoch_conservation(economic_world):
    w = economic_world
    people = [w.player(), w.player()]
    for week, epoch in enumerate(w.epochs):
        for p in people:
            two_days(w, p, week)
        w.freeze(epoch)
        if week == 0:
            with pytest.raises(Exception, match="gate"):
                settle_epoch(epoch, at=epoch.ends_at)
        w.clock["at"] = max(epoch.ends_at, START + timedelta(days=14))
        settle_epoch(epoch, at=w.clock["at"])
        assert all(reconcile_epoch(epoch).values())
        budget = EpochCategoryBudget.objects.get(epoch=epoch, category=CATEGORIES[1])
        assert budget.allocated_amount == (3800 if week == 0 else 4750)
        empty = EpochCategoryBudget.objects.get(epoch=epoch, category=CATEGORIES[0])
        assert empty.allocated_amount == 0
        assert empty.carry_out == (week + 1) * 3_325_000
        assert settle_epoch(epoch, at=w.clock["at"]).pk == epoch.pk
    assert not reconcile_season_dlx(w.season)["correction_admission_closed"]
    w.clock["at"] = w.season.ends_at
    for epoch in w.epochs:
        for category in CATEGORIES:
            finalize_correction_round(epoch, category, actor=w.actor)
    result = reconcile_season_dlx(w.season, record_residuals=True)
    assert result["reconciled"] and result["authorized_budget"] == 60_000_000
    assert result["unused_correction_reserve"] == 3_000_000
    assert result["confirmed_allocations"] + result["final_residual"] == 60_000_000
    assert DlxAllocation.objects.filter(season=w.season).count() == 24


@pytest.mark.django_db
def test_funded_correction_cohort_exact_replay_holds_and_immutable_history(economic_world):
    w = economic_world
    original = w.player()
    omitted = [w.player(), w.player()]
    two_days(w, original)
    settle_first(w)
    before = list(DlxAllocation.objects.values_list("pk", "amount"))
    for p in omitted:
        correction_case(w, p)
    cases = EconomicDeferral.objects.filter(epoch=w.epochs[0], category=CATEGORIES[1])
    for c in cases:
        assess_correction(
            c,
            performance_units=325,
            actor=w.actor,
            evidence_reference="reviewed complete canonical score",
        )
    w.clock["at"] = w.season.ends_at
    row = finalize_correction_round(w.epochs[0], CATEGORIES[1], actor=w.actor)
    assert len(row.calculation["corrected_scores"]) == 3
    assert finalize_correction_round(w.epochs[0], CATEGORIES[1], actor=w.actor).pk == row.pk
    results = pay_correction_round(row, actor=w.actor, idempotency_key="payment")
    assert {r.outcome for r in results} == {"PAID"}
    assert [r.pk for r in pay_correction_round(row, actor=w.actor, idempotency_key="payment")] == [
        r.pk for r in results
    ]
    assert all(
        r.outcome == "ALREADY_PAID"
        for r in pay_correction_round(row, actor=w.actor, idempotency_key="another-payment")
    )
    payment = DlxCorrection.objects.filter(entitlement__player=omitted[0]).get()
    assert 0 < payment.amount <= 20_000
    adjustment = record_correction_adjustment(
        payment,
        kind="HOLD",
        amount=payment.amount,
        actor=w.actor,
        reason="review",
        evidence_reference="case",
        idempotency_key="hold",
    )
    assert (
        record_correction_adjustment(
            payment,
            kind="HOLD",
            amount=payment.amount,
            actor=w.actor,
            reason="review",
            evidence_reference="case",
            idempotency_key="hold",
        ).pk
        == adjustment.pk
    )
    assert player_dlx_balance(omitted[0], w.season)["held"] == payment.amount
    assert list(DlxAllocation.objects.values_list("pk", "amount")) == before
    with pytest.raises(DatabaseError), transaction.atomic():
        DlxCorrection.objects.filter(pk=payment.pk).update(amount=1)


@pytest.mark.django_db
def test_insufficient_reserve_and_minimum_score_are_not_minting(economic_world):
    w = economic_world
    p = w.player()
    two_days(w, p)
    settle_first(w)
    omitted = [w.player() for _ in range(60)]
    for person in omitted:
        correction_case(w, person)
    for case in EconomicDeferral.objects.filter(epoch=w.epochs[0], category=CATEGORIES[1]):
        with pytest.raises(ValidationError, match="NOT_REPRODUCIBLE"):
            assess_correction(
                case,
                performance_units=99,
                actor=w.actor,
                evidence_reference="unsupported lower score",
            )
        assess_correction(
            case,
            performance_units=325,
            actor=w.actor,
            evidence_reference="canonical complete reviewed score",
        )
    w.clock["at"] = w.season.ends_at
    row = finalize_correction_round(w.epochs[0], CATEGORIES[1], actor=w.actor)
    assert all(v >= 100 for v in row.calculation["corrected_scores"].values())
    results = pay_correction_round(row, actor=w.actor, idempotency_key="underfunded")
    assert all(r.outcome == "INSUFFICIENT_RESERVE" for r in results)
    assert not DlxCorrection.objects.exists()
    assert EconomicDeferral.objects.filter(resolution__isnull=True).exists()
    assert (
        CategoryCorrectionReserve.objects.get(
            budget__epoch=w.epochs[0], budget__category=CATEGORIES[1]
        ).amount
        == 100_000
    )


@pytest.mark.django_db
def test_worker_crash_rolls_back_all_allocations_then_replays(economic_world):
    w = economic_world
    p = w.player()
    two_days(w, p)
    w.freeze(w.epochs[0])
    w.clock["at"] = START + timedelta(days=14)
    with (
        patch(
            "apps.arena.epochs.DlxAllocation.objects.bulk_create",
            side_effect=RuntimeError("worker crash"),
        ),
        pytest.raises(RuntimeError, match="worker crash"),
    ):
        settle_epoch(w.epochs[0], at=w.clock["at"])
    assert not DlxAllocation.objects.exists()
    w.epochs[0].refresh_from_db()
    assert w.epochs[0].state == "FROZEN"
    settle_epoch(w.epochs[0], at=w.clock["at"])
    assert all(reconcile_epoch(w.epochs[0]).values())


@pytest.mark.django_db
def test_database_old_writer_and_recognition_scope_guards(economic_world):
    w = economic_world
    p = w.player()
    attempt = w.typing(p, START + timedelta(days=1))
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('dexo.s1_source_writer', '', true)")
    with pytest.raises(DatabaseError, match="immutable|compatible fenced"), transaction.atomic():
        from apps.typing_game.models import TypingAttempt

        TypingAttempt.objects.filter(pk=attempt.pk).update(wpm=1)
    receipt = EconomicRecognition.objects.get(player=p)
    with pytest.raises(DatabaseError), transaction.atomic():
        EconomicRecognition.objects.filter(pk=receipt.pk).delete()
    outsider = User.objects.create_user("unauthorized")
    with pytest.raises(PermissionDenied):
        assess_correction(
            correction_case(w, p),
            performance_units=325,
            actor=outsider,
            evidence_reference="untrusted",
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_freeze_and_settlement_workers(economic_world):
    w = economic_world
    p = w.player()
    two_days(w, p)
    w.clock["at"] = START + timedelta(days=14)
    prepare_epoch(w.epochs[0])
    evaluate_epoch_readiness(w.epochs[0], evaluation_key="concurrent")

    def run_pair(action):
        barrier = Barrier(2)

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return action().pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(lambda _: worker(), range(2)))

    assert run_pair(lambda: freeze_epoch(w.epochs[0], at=w.clock["at"])) == [w.epochs[0].pk] * 2
    assert run_pair(lambda: settle_epoch(w.epochs[0], at=w.clock["at"])) == [w.epochs[0].pk] * 2
    assert DlxAllocation.objects.count() == 2
    assert all(reconcile_epoch(w.epochs[0]).values())


@pytest.mark.django_db
def test_week_six_late_work_has_funded_case_and_no_epoch_seven(economic_world):
    w = economic_world
    p = w.player()
    source = w.epochs[5].ends_at - timedelta(seconds=1)
    w.clock["at"] = w.epochs[5].ends_at + timedelta(hours=1)
    with transaction.atomic():
        record = record_recognition(
            season_id=w.season.pk,
            player_id=p.pk,
            actor_id=p.pk,
            source_type="COMMUNITY_ACTIVITY",
            source_id="clan:final-week-late",
            source_time=source,
            finalized_at=w.clock["at"],
            config_version_id=w.config.pk,
            inputs={"activity_id": 123},
            delayed=True,
        )
    assert record is None
    case = EconomicDeferral.objects.get(provider="COMMUNITY_ACTIVITY")
    assert case.epoch_id == w.epochs[5].pk and case.reason == "FUNDED_CORRECTION_REQUIRED"
    assert case.evidence["source_time"] == source.isoformat() and len(w.epochs) == 6


@pytest.mark.django_db
def test_later_proof_appends_assessment_revision_and_unknown_raw_case_blocks_round(economic_world):
    w = economic_world
    good, late = w.player(), w.player()
    two_days(w, good)
    settle_first(w)
    case = correction_case(w, late)
    first = assess_correction(
        case, performance_units=325, actor=w.actor, evidence_reference="first complete proof"
    )
    w.typing(late, START + timedelta(days=3))
    second = assess_correction(
        case, performance_units=488, actor=w.actor, evidence_reference="additional canonical proof"
    )
    assert second.pk != first.pk and case.assessments.count() == 2
    for c in EconomicDeferral.objects.filter(epoch=w.epochs[0], category=CATEGORIES[1]).exclude(
        pk=case.pk
    ):
        assess_correction(
            c, performance_units=488, actor=w.actor, evidence_reference="final complete proof"
        )
    raw = defer_source(
        w.epochs[0],
        "TELEGRAM_INBOUND",
        "unscoped-potential-source",
        reason="NOT_FINALIZED",
        player_id=late.pk,
    )
    w.clock["at"] = w.season.ends_at
    with pytest.raises(ValidationError, match="SOURCE_UNIVERSE_NOT_FINALIZED"):
        finalize_correction_round(w.epochs[0], CATEGORIES[1], actor=w.actor)
    from apps.arena.economics import resolve_nonperformance_case

    resolve_nonperformance_case(
        raw,
        actor=w.actor,
        reason="audited non-economic update",
        evidence_reference="canonical dispatcher evidence",
    )
    row = finalize_correction_round(w.epochs[0], CATEGORIES[1], actor=w.actor)
    assert row.calculation["corrected_scores"][str(late.pk)] == 488


@pytest.mark.django_db
def test_unproven_source_cannot_be_closed_using_baseline_score(economic_world):
    w = economic_world
    p = w.player()
    two_days(w, p)
    settle_first(w)
    case = defer_source(
        w.epochs[0],
        "BASE_TAP_BATCH",
        "missing-accepted-count",
        reason="HISTORICAL_TAP_COUNT_UNPROVEN",
        player_id=p.pk,
        category=CATEGORIES[1],
        evidence={"fact_id": 123},
    )
    with pytest.raises(ValidationError, match="SOURCE_PROOF_UNRESOLVED"):
        assess_correction(
            case,
            performance_units=325,
            actor=w.actor,
            evidence_reference="baseline is not missing proof",
        )


@pytest.mark.django_db
def test_proven_one_day_late_beneficiary_has_audited_zero_entitlement(economic_world):
    from apps.arena.corrections import resolve_ineligible_correction

    w = economic_world
    good, late = w.player(), w.player()
    two_days(w, good)
    settle_first(w)
    w.typing(late, START + timedelta(days=1))
    case = EconomicDeferral.objects.get(player=late, provider="TYPING_CREDIT")
    resolution = resolve_ineligible_correction(
        case,
        actor=w.actor,
        evidence_reference="only one meaningful day, all scoped sources finalized",
    )
    assert resolution.outcome == "NO_ELIGIBLE_ENTITLEMENT" and case.proofs.exists()
    assert not DlxCorrection.objects.filter(entitlement__player=late).exists()


@pytest.mark.django_db
def test_correction_cannot_replay_source_recognized_in_another_epoch(economic_world):
    from apps.arena.corrections import corrected_category_scores
    from apps.arena.policy import digest

    w = economic_world
    p = w.player()
    two_days(w, p)
    settle_first(w)
    for index in range(2):
        source = START + timedelta(days=3, seconds=index)
        finalized = START + timedelta(days=8, seconds=index)
        inputs = {"activity_id": 10000 + index}
        identity = f"clan:handoff:{index}"
        proof = {
            "source_time": source.isoformat(),
            "finalized_at": finalized.isoformat(),
            "config_version_id": w.config.pk,
            "actor_id": p.pk,
            "inputs": inputs,
            "evidence_digest": digest(
                {
                    "season": w.season.pk,
                    "player": p.pk,
                    "actor": p.pk,
                    "source_time": source.isoformat(),
                    "config": w.config.pk,
                    "inputs": inputs,
                    "category": CATEGORIES[4],
                }
            ),
        }
        defer_source(
            w.epochs[0],
            "COMMUNITY_ACTIVITY",
            identity,
            reason="DEFERRED",
            player_id=p.pk,
            category=CATEGORIES[4],
            evidence=proof,
        )
        with transaction.atomic():
            record_recognition(
                season_id=w.season.pk,
                player_id=p.pk,
                actor_id=p.pk,
                source_type="COMMUNITY_ACTIVITY",
                source_id=identity,
                source_time=source,
                finalized_at=finalized,
                config_version_id=w.config.pk,
                inputs=inputs,
                delayed=True,
            )
    assert corrected_category_scores(w.epochs[0])[p.pk][CATEGORIES[4]][0] == 2857
