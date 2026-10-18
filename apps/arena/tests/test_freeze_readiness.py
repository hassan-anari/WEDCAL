# mypy: disable-error-code="no-untyped-def"
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from io import StringIO
from threading import Event

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone

from apps.arena.epochs import EpochBlocked, drain_blockers, freeze_epoch, initialize_epochs
from apps.arena.models import (
    EconomicEpoch,
    EpochCategoryBudget,
    EpochPerformanceSnapshot,
    QualificationDecision,
)
from apps.arena.qualification import record_qualification_decision
from apps.arena.readiness import (
    APPROVED_EVALUATORS,
    ApprovedEvaluator,
    evaluate_epoch_readiness,
    policy_identity,
)
from apps.seasons.barrier import acquire_economic_write_barrier
from apps.telegram.models import TelegramInboundUpdate


@pytest.mark.django_db
def test_missing_qualification_completion_cannot_freeze(phase4_config_factory, season_factory):
    epoch = initialize_epochs(season_factory(phase4_config_factory()))[0]
    with pytest.raises(EpochBlocked, match="QUALIFICATION_NOT_EVALUATED"):
        freeze_epoch(epoch, at=epoch.ends_at)
    epoch.refresh_from_db()
    assert epoch.state == "DRAINING"
    assert epoch.frozen_at is None and epoch.snapshots_completed_at is None
    assert not EpochCategoryBudget.objects.filter(epoch=epoch).exists()
    assert not EpochPerformanceSnapshot.objects.filter(epoch=epoch).exists()


@pytest.fixture
def epoch(phase4_config_factory, season_factory):
    return initialize_epochs(season_factory(phase4_config_factory()))[0]


def decision(player, epoch, **overrides) -> QualificationDecision:
    return record_qualification_decision(
        **{
            "player": player,
            "policy_key": "QUALIFIED_PLAYER",
            "policy_version": 1,
            "decision": "QUALIFIED",
            "effective_at": epoch.starts_at,
            "reason_code": "EXPLICIT_SYNTHETIC_REVIEW",
            "source": "TEST",
            "source_id": f"qualification:{player.pk}",
            **overrides,
        }
    )[0]


def assert_no_final_outputs(epoch) -> None:
    epoch.refresh_from_db()
    assert epoch.state in {"OPEN", "DRAINING"}
    assert epoch.frozen_at is None and epoch.snapshots_completed_at is None
    assert not epoch.category_budgets.exists()
    assert not epoch.performance_snapshots.exists()


@pytest.mark.django_db
def test_activity_window_stays_open(epoch):
    with pytest.raises(EpochBlocked, match="has not ended"):
        freeze_epoch(epoch, at=epoch.ends_at - timedelta(microseconds=1))
    assert_no_final_outputs(epoch)
    assert epoch.state == "OPEN"


@pytest.mark.django_db
def test_positive_decisions_are_not_completeness(epoch, player_factory):
    decision(player_factory(), epoch)
    with pytest.raises(EpochBlocked, match="QUALIFICATION_NOT_EVALUATED"):
        freeze_epoch(epoch, at=epoch.ends_at)
    assert_no_final_outputs(epoch)


@pytest.mark.django_db
def test_production_evaluator_is_explicitly_unconfigured(epoch):
    assert policy_identity(epoch) not in APPROVED_EVALUATORS
    run = evaluate_epoch_readiness(epoch, evaluation_key="unconfigured")
    assert run.status == "BLOCKED"
    assert set(run.blocking_reasons) == {
        "QUALIFICATION_EVALUATOR_UNCONFIGURED",
        "SOURCE_COMPLETION_UNVERIFIED",
    }
    with pytest.raises(EpochBlocked, match="QUALIFICATION_BLOCKED"):
        freeze_epoch(epoch, at=epoch.ends_at)
    assert_no_final_outputs(epoch)


@pytest.mark.django_db
def test_partial_evaluation_cannot_be_complete(epoch, ready_epoch, monkeypatch):
    original = APPROVED_EVALUATORS[policy_identity(epoch)]
    monkeypatch.setitem(
        APPROVED_EVALUATORS,
        policy_identity(epoch),
        ApprovedEvaluator(
            "partial",
            lambda row: replace(
                original.inspect(row), scope_count=2, evaluated_count=1, unresolved_count=1
            ),
        ),
    )
    run = evaluate_epoch_readiness(epoch, evaluation_key="partial")
    assert run.status == "BLOCKED" and "QUALIFICATION_INCOMPLETE" in run.blocking_reasons
    with pytest.raises(EpochBlocked, match="QUALIFICATION_BLOCKED"):
        freeze_epoch(epoch, at=epoch.ends_at)
    assert_no_final_outputs(epoch)


@pytest.mark.django_db(transaction=True)
def test_failed_evaluation_rolls_back_and_persists_failure(epoch, ready_epoch, monkeypatch):
    def fail(_row):
        raise RuntimeError("simulated evaluation failure")

    monkeypatch.setitem(
        APPROVED_EVALUATORS, policy_identity(epoch), ApprovedEvaluator("failed", fail)
    )
    with pytest.raises(RuntimeError, match="simulated"):
        evaluate_epoch_readiness(epoch, evaluation_key="failed")
    run = epoch.evaluations.get()
    assert run.status == "FAILED" and run.completed_at is not None
    assert run.evidence == {} and not QualificationDecision.objects.exists()
    with pytest.raises(EpochBlocked, match="QUALIFICATION_FAILED"):
        freeze_epoch(epoch, at=epoch.ends_at)
    assert_no_final_outputs(epoch)


@pytest.mark.django_db(transaction=True)
def test_interrupted_evaluation_resumes_same_identity(epoch, ready_epoch, monkeypatch):
    original = APPROVED_EVALUATORS[policy_identity(epoch)]

    def crash(_row):
        raise SystemExit("simulated process loss")

    monkeypatch.setitem(
        APPROVED_EVALUATORS, policy_identity(epoch), ApprovedEvaluator("crash", crash)
    )
    with pytest.raises(SystemExit):
        evaluate_epoch_readiness(epoch, evaluation_key="interrupted")
    run = epoch.evaluations.get()
    assert run.status == "RUNNING" and run.completed_at is None
    with pytest.raises(EpochBlocked, match="QUALIFICATION_RUNNING"):
        freeze_epoch(epoch, at=epoch.ends_at)
    monkeypatch.setitem(APPROVED_EVALUATORS, policy_identity(epoch), original)
    resumed = evaluate_epoch_readiness(epoch, evaluation_key="interrupted")
    assert resumed.pk == run.pk and resumed.status == "COMPLETE"
    assert evaluate_epoch_readiness(epoch, evaluation_key="interrupted").pk == run.pk
    freeze_epoch(epoch, at=epoch.ends_at)
    assert epoch.evaluations.count() == 1


@pytest.mark.django_db
def test_authoritatively_empty_synthetic_epoch_and_replay(epoch, ready_epoch):
    run = ready_epoch(epoch)
    assert run.evidence["scope_count"] == run.evidence["evaluated_count"] == 0
    for _ in range(3):
        assert freeze_epoch(epoch, at=epoch.ends_at).state == "FROZEN"
    assert epoch.category_budgets.count() == 5
    assert epoch.performance_snapshots.count() == 0
    assert epoch.evaluations.count() == 1
    assert not QualificationDecision.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["ACTIVE", "FLAGGED", "SUSPENDED", "BANNED"])
def test_qualified_and_moderated_players_preserve_cutoff_behavior(
    epoch, player_factory, ready_epoch, state
):
    player = player_factory()
    player.moderation_state = state
    player.save(update_fields=["moderation_state"])
    decision(player, epoch)
    ready_epoch(epoch)
    freeze_epoch(epoch, at=epoch.ends_at)
    assert epoch.performance_snapshots.count() == (5 if state in {"ACTIVE", "FLAGGED"} else 0)


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["PENDING", "PROCESSING", "FAILED", "DEAD_LETTER"])
def test_late_telegram_and_dead_letters_block_without_losing_work(epoch, ready_epoch, state):
    ready_epoch(epoch)
    inbound = TelegramInboundUpdate.objects.create(
        update_id=801,
        payload={"private": "retained"},
        state=state,
        payload_expires_at=timezone.now() + timedelta(days=1),
    )
    assert inbound.received_at > epoch.ends_at
    with pytest.raises(EpochBlocked, match="DRAIN_TELEGRAM_INBOUND"):
        freeze_epoch(epoch, at=epoch.ends_at)
    assert_no_final_outputs(epoch)
    inbound.refresh_from_db()
    assert inbound.payload == {"private": "retained"} and inbound.state == state
    TelegramInboundUpdate.objects.filter(pk=inbound.pk).update(state="IGNORED")
    freeze_epoch(epoch, at=epoch.ends_at)


@pytest.mark.django_db
def test_decision_after_evaluation_invalidates_receipt(epoch, ready_epoch, player_factory):
    player = player_factory()
    ready_epoch(epoch)
    decision(player, epoch)
    with pytest.raises(EpochBlocked, match="QUALIFICATION_EVIDENCE_STALE"):
        freeze_epoch(epoch, at=epoch.ends_at)
    assert_no_final_outputs(epoch)
    ready_epoch(epoch)
    freeze_epoch(epoch, at=epoch.ends_at)
    assert epoch.performance_snapshots.count() == 5
    with pytest.raises(ValueError, match="QUALIFICATION_CUTOFF_FINALIZED"):
        decision(player, epoch, source_id="new-retrospective-decision", decision="DISQUALIFIED")
    assert decision(player, epoch).pk == QualificationDecision.objects.get().pk


@pytest.mark.django_db(transaction=True)
def test_snapshot_database_failure_is_atomic_then_retry(
    epoch, ready_epoch, player_factory, monkeypatch
):
    decision(player_factory(), epoch)
    ready_epoch(epoch)
    original = EpochPerformanceSnapshot.objects.create
    count = 0

    def fail_second(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1/0")
        return original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(EpochPerformanceSnapshot.objects, "create", fail_second)
        with pytest.raises(DatabaseError):
            freeze_epoch(epoch, at=epoch.ends_at)
    assert_no_final_outputs(epoch)
    freeze_epoch(epoch, at=epoch.ends_at)
    assert epoch.performance_snapshots.count() == 5 and epoch.category_budgets.count() == 5


@pytest.mark.django_db(transaction=True)
def test_old_worker_and_raw_sql_cannot_bypass_readiness(epoch):
    with pytest.raises(DatabaseError, match="readiness"), transaction.atomic():
        EconomicEpoch.objects.filter(pk=epoch.pk).update(state="FROZEN")
    assert_no_final_outputs(epoch)


@pytest.mark.django_db(transaction=True)
def test_completed_evidence_is_database_immutable(epoch, ready_epoch):
    run = ready_epoch(epoch)
    with pytest.raises(DatabaseError), transaction.atomic():
        epoch.evaluations.update(status="RUNNING", completed_at=None)
    with pytest.raises(DatabaseError), transaction.atomic():
        epoch.evaluations.all().delete()
    run.refresh_from_db()
    assert run.status == "COMPLETE"


@pytest.mark.django_db(transaction=True)
def test_concurrent_freeze_workers_are_idempotent(epoch, ready_epoch):
    ready_epoch(epoch)

    def freeze() -> str:
        close_old_connections()
        try:
            return freeze_epoch(EconomicEpoch.objects.get(pk=epoch.pk), at=epoch.ends_at).state
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: freeze(), range(2))) == ["FROZEN", "FROZEN"]
    assert epoch.category_budgets.count() == 5 and epoch.evaluations.count() == 1


@pytest.mark.django_db(transaction=True)
def test_freeze_waits_for_canonical_economic_writer(epoch, ready_epoch):
    ready_epoch(epoch)
    locked, release, frozen = Event(), Event(), Event()

    def writer():
        close_old_connections()
        try:
            with transaction.atomic():
                acquire_economic_write_barrier(epoch.season_id)
                locked.set()
                assert release.wait(5)
        finally:
            close_old_connections()

    def freeze():
        close_old_connections()
        try:
            freeze_epoch(epoch, at=epoch.ends_at)
            frozen.set()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(writer)
        assert locked.wait(5)
        second = pool.submit(freeze)
        assert not frozen.wait(0.2)
        release.set()
        first.result(5)
        second.result(5)
    assert frozen.is_set()


@pytest.mark.django_db(transaction=True)
def test_preflight_is_database_read_only_json_and_zero_is_not_ready(epoch):
    import json

    output = StringIO()
    writes = []

    def forbid_writes(execute, sql, params, many, context):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(sql)
            raise AssertionError("preflight attempted mutation")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(forbid_writes), pytest.raises(CommandError) as exc:
        call_command("epoch_freeze_preflight", epoch.season.code, epoch.epoch_number, stdout=output)
    assert exc.value.returncode == 2 and writes == []
    report = json.loads(output.getvalue())
    assert report["readiness"] == "NOT_EVALUATED" and report["economic_activity_players"] == 0
    assert "QUALIFICATION_NOT_EVALUATED" in report["blocking_reasons"]
    assert "SOURCE_COMPLETION_UNVERIFIED" in report["blocking_reasons"]
    epoch.refresh_from_db()
    assert epoch.state == "OPEN" and epoch.freeze_watermark == {}
    assert not epoch.evaluations.exists() and not QualificationDecision.objects.exists()


@pytest.mark.django_db
def test_clan_windows_unknown_end_and_mission_signals_hold_until_durable_completion(clan_world):
    from apps.clans.models import ClanActivityWindow, ClanMembership, ClanMissionSignal

    now, config, season, _, _, clan_factory = clan_world
    clan = clan_factory()
    membership = ClanMembership.objects.get(clan_season=clan)
    first, second = initialize_epochs(season)[:2]
    # A UTC aggregation window may overlap an arbitrary Season-relative weekly boundary.
    window = ClanActivityWindow.objects.create(
        clan_season=clan,
        membership=membership,
        season=season,
        config_version=config,
        window_start=first.ends_at - timedelta(seconds=20),
        window_end=None,
    )
    signal = ClanMissionSignal.objects.create(
        membership=membership,
        source_key="delayed",
        requirement="VALID_TYPING_RUNS",
        units=1,
        occurred_at=first.ends_at - timedelta(microseconds=1),
        requires_review=True,
    )
    assert {x.provider for x in drain_blockers(second)} >= {"CLAN_WINDOWS", "CLAN_MISSION_SIGNALS"}
    window.finalized_at = now
    window.save(update_fields=["finalized_at"])
    signal.processed_at = now
    signal.save(update_fields=["processed_at"])
    assert {x.provider for x in drain_blockers(second)}.isdisjoint(
        {"CLAN_WINDOWS", "CLAN_MISSION_SIGNALS"}
    )


@pytest.mark.django_db
def test_network_pending_source_boundary_is_half_open(epoch, player_factory):
    from apps.network.models import CreatorGroupRegistration, NetworkActivity
    from apps.telegram.models import TelegramGroup

    player = player_factory()
    group = TelegramGroup.objects.create(
        telegram_chat_id=-99,
        first_seen_at=epoch.starts_at,
        last_seen_at=epoch.starts_at,
        chat_type="supergroup",
    )
    registration = CreatorGroupRegistration.objects.create(creator=player, group=group)
    activity = NetworkActivity.objects.create(
        registration=registration,
        group=group,
        sender=player,
        season=epoch.season,
        config_version=epoch.config_version,
        telegram_message_id=1,
        telegram_update_id=1,
        observed_at=epoch.ends_at,
        classification="VALID",
        content_expires_at=timezone.now(),
        fingerprint_expires_at=timezone.now(),
    )
    assert "NETWORK_ACTIVITY" not in {x.provider for x in drain_blockers(epoch)}
    second = EconomicEpoch.objects.get(season=epoch.season, epoch_number=2)
    assert "NETWORK_ACTIVITY" in {x.provider for x in drain_blockers(second)}
    activity.observed_at = epoch.ends_at - timedelta(microseconds=1)
    activity.save(update_fields=["observed_at"])
    assert "NETWORK_ACTIVITY" in {x.provider for x in drain_blockers(epoch)}


@pytest.mark.django_db
def test_changed_pinned_configuration_cannot_reuse_evaluation(
    epoch, ready_epoch, phase4_config_factory
):
    ready_epoch(epoch)
    other = phase4_config_factory(version="next", effective_at=epoch.ends_at + timedelta(days=1))
    EconomicEpoch.objects.filter(pk=epoch.pk).update(config_version=other)
    with pytest.raises(EpochBlocked, match="QUALIFICATION_PROVENANCE_MISMATCH"):
        freeze_epoch(epoch, at=epoch.ends_at)
    assert_no_final_outputs(epoch)


@pytest.mark.django_db
def test_expected_worker_blockers_are_safe_and_repeatable(epoch):
    from apps.arena.tasks import drain_and_freeze_epochs, settle_epochs

    for _ in range(2):
        assert drain_and_freeze_epochs() == 0
    assert_no_final_outputs(epoch)
    assert "QUALIFICATION_NOT_EVALUATED" in epoch.freeze_watermark["blockers"]
    assert settle_epochs() == 0
    assert not epoch.evaluations.exists()


@pytest.mark.django_db(transaction=True)
def test_qualification_writer_serializes_with_final_freeze(
    epoch, ready_epoch, player_factory, monkeypatch
):
    player = player_factory()
    ready_epoch(epoch)
    entered, release = Event(), Event()
    from apps.arena import epochs

    original = epochs._snapshot_budgets

    def hold(row, policy) -> None:
        entered.set()
        assert release.wait(5)
        original(row, policy)

    monkeypatch.setattr(epochs, "_snapshot_budgets", hold)

    def freeze() -> None:
        close_old_connections()
        try:
            freeze_epoch(epoch, at=epoch.ends_at)
        finally:
            close_old_connections()

    def qualify() -> str:
        close_old_connections()
        try:
            decision(player, epoch)
            return "UNSAFE"
        except ValueError as exc:
            return str(exc)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        freezer = pool.submit(freeze)
        assert entered.wait(5)
        writer = pool.submit(qualify)
        assert not writer.done()
        release.set()
        freezer.result(5)
        assert writer.result(5) == "QUALIFICATION_CUTOFF_FINALIZED"
    assert not QualificationDecision.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_preflight_counts_latest_policy_and_moderation_without_exposing_identities(
    epoch, player_factory
):
    import json

    banned, revoked = player_factory(100001), player_factory(100002)
    decision(banned, epoch)
    banned.moderation_state = "BANNED"
    banned.save(update_fields=["moderation_state"])
    decision(revoked, epoch)
    decision(
        revoked,
        epoch,
        source_id="revocation",
        decision="DISQUALIFIED",
        effective_at=epoch.starts_at + timedelta(seconds=1),
    )
    # An unrelated policy version must not become the Epoch's authoritative decision.
    decision(revoked, epoch, source_id="other-policy", policy_version=2)
    output = StringIO()
    with pytest.raises(CommandError):
        call_command("epoch_freeze_preflight", epoch.season.code, epoch.epoch_number, stdout=output)
    report = json.loads(output.getvalue())
    assert report["qualified_before_moderation"] == 1
    assert report["qualified_players"] == 0
    assert report["excluded_or_disqualified_players"] == 2
    assert report["unresolved_eligibility"] is None
    assert str(banned.telegram_user_id) not in output.getvalue()
    assert str(revoked.telegram_user_id) not in output.getvalue()


@pytest.mark.django_db
def test_season_close_marker_does_not_disable_audited_moderation(epoch, player_factory):
    from uuid import uuid4

    from apps.moderation.models import ModerationCase
    from apps.moderation.services import apply_action
    from apps.seasons.models import SeasonCloseRun

    target, reporter = player_factory(18001), player_factory(18002)
    case = ModerationCase.objects.create(
        reporter=reporter,
        target=target,
        category="FRAUD",
        reason="synthetic review",
        config_version=epoch.config_version,
    )
    run = SeasonCloseRun.objects.create(
        season=epoch.season,
        idempotency_key="synthetic-close",
        cutoff_at=epoch.ends_at,
        service_actor="TEST",
        reason="synthetic close",
        correlation_id=uuid4(),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('dexo.season_close_barrier', 'on', true)")
        SeasonCloseRun.objects.filter(pk=run.pk).update(barrier_established_at=timezone.now())
        cursor.execute("SELECT set_config('dexo.season_close_barrier', 'off', true)")
    action = apply_action(
        case_id=case.pk,
        action_type="BAN",
        reason="synthetic review",
        idempotency_key="synthetic-ban",
        system_actor="TEST",
    )
    target.refresh_from_db()
    assert target.moderation_state == "BANNED" and action.target_id == target.pk
