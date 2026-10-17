# mypy: disable-error-code="no-untyped-def,no-untyped-call"
"""Source-time lifecycle and real PostgreSQL locking regressions (synthetic only)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Event
from time import monotonic

import pytest
from django.contrib.auth.models import User
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import Client
from django.utils import timezone

from apps.clans import missions
from apps.clans.economy import settle_clan_reward
from apps.clans.models import (
    ClanMembership,
    ClanMembershipEvent,
    ClanMissionActivation,
    ClanMissionDefinition,
    ClanMissionProgress,
    ClanMissionSignal,
    ClanMissionSourceEvent,
    ClanRole,
)
from apps.clans.tests.test_b6_admin import control
from apps.notifications.models import OperatorAuditEvent
from apps.seasons.close import _clan_mission_signals

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def lifecycle(client, clan_world):
    now, config, season, resolved, player, clan = clan_world
    actor = User.objects.create_superuser("lifecycle-operator")
    client.force_login(actor)
    cs = clan()
    member = cs.memberships.get()
    ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)

    def definition(**changes):
        return ClanMissionDefinition.objects.create(
            **{
                "key": f"lifecycle-{ClanMissionDefinition.objects.count()}",
                "version": 1,
                "title": "Verified source work",
                "kind": "GROUP",
                "requirement": "VALID_TYPING_RUN",
                "target_units": 1,
                "reward_band": "GROUP",
                "starts_at": now - timedelta(hours=1),
                "ends_at": now + timedelta(hours=2),
                "config_version": config,
                **changes,
            }
        )

    return now, config, season, cs, member, definition, player, resolved, actor


def toggle(client, definition):
    assert control(client, "clans/clanmissiondefinition", definition).status_code == 302
    definition.refresh_from_db()
    return definition.active_intervals.order_by("-pk").first()


def queue(member, key, at):
    missions.queue_mission_signal(
        membership=member, source_key=key, requirement="VALID_TYPING_RUN", units=1, occurred_at=at
    )


@pytest.mark.parametrize("kind,band", [("GROUP", "GROUP"), ("INDIVIDUAL", "SMALL")])
def test_active_signal_completes_after_deactivation_and_replay_is_immutable(
    client, lifecycle, kind, band
):
    _, _, _, cs, member, create, *_ = lifecycle
    definition = create(kind=kind, reward_band=band)
    interval = toggle(client, definition)
    queue(member, "active-then-disabled", interval.starts_at)
    toggle(client, definition)
    assert not definition.enabled
    assert missions.process_mission_signals() == 1
    progress = ClanMissionProgress.objects.get()
    source = ClanMissionSourceEvent.objects.get()
    assert source.activation_id == interval.pk
    assert progress.completed_at == interval.starts_at and progress.reward_settlement_id
    settlement = progress.reward_settlement
    assert settlement is not None
    assert settlement.player_amount + settlement.treasury_amount == settlement.gross_amount
    before = (cs.economic_settlements.count(), cs.hq_xp_entries.count(), progress.units)
    # A committed source remains replayable even though the definition is disabled.
    assert (
        missions.record_mission_progress(
            definition=definition,
            membership=member,
            source_id=source.source_id,
            units=1,
            occurred_at=source.occurred_at,
        ).pk
        == progress.pk
    )
    ClanMissionSignal.objects.update(processed_at=None)  # Simulate lost task ACK/retry.
    assert missions.process_mission_signals() == 1
    assert missions.process_mission_signals() == 0
    progress.refresh_from_db()
    assert (cs.economic_settlements.count(), cs.hq_xp_entries.count(), progress.units) == before


def test_pre_activation_signal_is_terminal_without_progress_or_retry(client, lifecycle):
    now, _, _, cs, member, create, *_ = lifecycle
    definition = create()
    queue(member, "before-first-activation", now)
    toggle(client, definition)
    assert missions.process_mission_signals() == 1
    signal = ClanMissionSignal.objects.get()
    assert signal.processed_at and signal.attempts == 0 and not signal.requires_review
    assert not ClanMissionProgress.objects.exists() and not cs.economic_settlements.exists()
    assert missions.process_mission_signals() == 0


@pytest.mark.parametrize("kind,band", [("GROUP", "GROUP"), ("INDIVIDUAL", "SMALL")])
def test_multiple_cycles_preserve_gaps_and_exact_activation_deactivation_boundaries(
    client, lifecycle, kind, band
):
    _, _, _, cs, member, create, *_ = lifecycle
    definition = create(kind=kind, reward_band=band, target_units=4)
    eligible = []
    for index in range(3):
        interval = toggle(client, definition)
        key = f"active-{index}"
        queue(member, key, interval.starts_at)  # Inclusive activation.
        eligible.append(key)
        closed = toggle(client, definition)
        assert closed.ends_at > closed.starts_at
        queue(member, f"gap-{index}", closed.ends_at)  # Exclusive deactivation.
    # Process all prior intervals only after reactivation. Inactive gaps stay excluded.
    last = toggle(client, definition)
    assert missions.process_mission_signals() == 6
    progress = ClanMissionProgress.objects.get()
    assert progress.units == 3 and progress.completed_at is None
    assert set(ClanMissionSourceEvent.objects.values_list("source_id", flat=True)) == set(eligible)
    assert not cs.economic_settlements.exists()
    queue(member, "active-final", last.starts_at)
    toggle(client, definition)
    assert missions.process_mission_signals() == 1
    progress.refresh_from_db()
    assert progress.units == 4 and progress.completed_at == last.starts_at
    assert cs.economic_settlements.count() == 1
    assert definition.active_intervals.count() == 4 and definition.activation_revision == 8


def test_exact_schedule_boundaries_remain_half_open(client, lifecycle):
    now, _, _, _, member, create, *_ = lifecycle
    start, end = now + timedelta(minutes=1), now + timedelta(minutes=2)
    definition = create(starts_at=start, ends_at=end, target_units=10)
    toggle(client, definition)
    for key, at in [
        ("before-schedule", start - timedelta(microseconds=1)),
        ("at-start", start),
        ("before-end", end - timedelta(microseconds=1)),
        ("at-end", end),
    ]:
        queue(member, key, at)
    assert missions.process_mission_signals() == 4
    assert ClanMissionProgress.objects.get().units == 2
    assert set(ClanMissionSourceEvent.objects.values_list("source_id", flat=True)) == {
        "at-start",
        "before-end",
    }


def test_before_activation_and_last_microsecond_before_deactivation(client, lifecycle):
    _, _, _, _, member, create, *_ = lifecycle
    definition = create(target_units=10)
    interval = toggle(client, definition)
    closed = toggle(client, definition)
    for key, at in [
        ("before", interval.starts_at - timedelta(microseconds=1)),
        ("start", interval.starts_at),
        ("last", closed.ends_at - timedelta(microseconds=1)),
        ("end", closed.ends_at),
    ]:
        queue(member, key, at)
    assert missions.process_mission_signals() == 4
    assert ClanMissionProgress.objects.get().units == 2
    assert set(ClanMissionSourceEvent.objects.values_list("source_id", flat=True)) == {
        "start",
        "last",
    }


def test_delayed_group_completion_only_pays_eligible_participants(client, lifecycle):
    now, config, season, cs, captain, create, player, *_ = lifecycle
    others = []
    for _ in range(3):
        member = ClanMembership.objects.create(
            player=player(ClanRole.SOLDIER),
            season=season,
            clan_season=cs,
            role="SOLDIER",
            started_at=now,
            config_version=config,
        )
        ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
        others.append(member)
    departed, soldier, nonparticipant = others
    definition = create(target_units=3)
    interval = toggle(client, definition)
    queue(departed, "departed-contribution", interval.starts_at)
    queue(captain, "captain-contribution", timezone.now())
    completion = timezone.now()
    ClanMembershipEvent.objects.create(
        membership=departed, kind="ACCESS_LOST", occurred_at=completion
    )
    queue(soldier, "soldier-completion", completion)
    toggle(client, definition)
    assert missions.process_mission_signals() == 3
    progress = ClanMissionProgress.objects.get()
    assert progress.units == 3 and progress.completed_at == completion
    assert progress.participants.get(membership=departed).reward_eligible is False
    assert set(
        progress.participants.filter(reward_settlement__isnull=False).values_list(
            "membership_id", flat=True
        )
    ) == {captain.pk, soldier.pk}
    assert not progress.participants.filter(membership=nonparticipant).exists()
    assert cs.economic_settlements.count() == 2
    for settlement in cs.economic_settlements.all():
        assert settlement.player_amount + settlement.treasury_amount == settlement.gross_amount


def test_transaction_failure_rolls_back_and_disabled_retry_settles_once(
    client, lifecycle, monkeypatch
):
    _, _, _, cs, member, create, *_ = lifecycle
    definition = create()
    interval = toggle(client, definition)
    queue(member, "retry-delayed", interval.starts_at)
    toggle(client, definition)
    settle = settle_clan_reward

    def fail_after_settlement(**kwargs):
        settle(**kwargs)
        raise RuntimeError("Synthetic post-settlement failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(missions, "settle_clan_reward", fail_after_settlement)
        assert missions.process_mission_signals() == 0
    signal = ClanMissionSignal.objects.get()
    assert signal.attempts == 1 and signal.processed_at is None
    assert not ClanMissionSourceEvent.objects.exists() and not ClanMissionProgress.objects.exists()
    assert not cs.economic_settlements.exists() and not cs.treasury_entries.exists()
    assert not cs.economic_accumulators.exists()
    ClanMissionSignal.objects.update(next_attempt_at=timezone.now())
    assert missions.process_mission_signals() == 1
    assert missions.process_mission_signals() == 0
    assert cs.economic_settlements.count() == 1


def test_full_cycle_invalidates_old_signed_preview_and_audit_identifies_interval(client, lifecycle):
    _, _, _, _, _, create, *_ = lifecycle
    definition = create()
    path = f"/admin/clans/clanmissiondefinition/{definition.pk}/control/"
    old_token = client.get(path).context["form"].initial["confirmation"]
    interval = toggle(client, definition)
    toggle(client, definition)
    result = client.post(
        path, {"confirmation": old_token, "reason": "stale cycle", "confirm": "on"}
    )
    assert result.status_code == 200
    definition.refresh_from_db()
    assert not definition.enabled and definition.activation_revision == 2
    records = OperatorAuditEvent.objects.filter(
        action__in=["MISSION_ACTIVATE", "MISSION_DEACTIVATE"]
    ).order_by("pk")
    interval.refresh_from_db()
    assert [a.after_state["activation_interval_id"] for a in records] == [interval.pk, interval.pk]
    assert [a.after_state["effective_at"] for a in records] == [
        interval.starts_at.isoformat(),
        interval.ends_at.isoformat(),
    ]


def test_database_owns_intervals_and_rolls_back_transitions(client, lifecycle):
    _, _, _, _, _, create, *_ = lifecycle
    definition = create()
    with pytest.raises(RuntimeError), transaction.atomic():
        ClanMissionDefinition.objects.filter(pk=definition.pk).update(enabled=True)
        assert ClanMissionActivation.objects.count() == 1
        raise RuntimeError("rollback activation")
    definition.refresh_from_db()
    assert not definition.enabled and definition.activation_revision == 0
    assert not ClanMissionActivation.objects.exists()
    interval = toggle(client, definition)
    changes_to_try: list[dict[str, object]] = [
        {"starts_at": timezone.now()},
        {"ends_at": timezone.now()},
        {"origin": "FORGED"},
    ]
    for changes in changes_to_try:
        with pytest.raises(DatabaseError), transaction.atomic():
            ClanMissionActivation.objects.filter(pk=interval.pk).update(**changes)
    with pytest.raises(DatabaseError), transaction.atomic():
        ClanMissionActivation.objects.filter(pk=interval.pk).delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        ClanMissionActivation.objects.create(
            definition=definition, starts_at=timezone.now(), origin="FORGED"
        )
    assert (
        client.get(f"/admin/clans/clanmissionactivation/{interval.pk}/change/").status_code == 200
    )
    assert (
        client.post(f"/admin/clans/clanmissionactivation/{interval.pk}/change/", {}).status_code
        == 403
    )


def _thread(call, pids):
    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pids.put(cursor.fetchone()[0])
        return call()
    finally:
        connection.close()


def _assert_blocked(waiter, blocker):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [blocker, waiter])
            if cursor.fetchone()[0]:
                return
        Event().wait(0.01)
    pytest.fail("Expected real PostgreSQL row-lock contention did not occur")


@pytest.mark.parametrize("admin_first", [True, False])
def test_concurrent_admin_deactivation_and_worker_preserve_source_eligibility(
    client, lifecycle, monkeypatch, admin_first
):
    _, _, _, cs, member, create, _, _, actor = lifecycle
    definition = create()
    interval = toggle(client, definition)
    queue(member, "concurrent-disable", interval.starts_at)
    held, release = Event(), Event()
    first_pid: Queue[int] = Queue()
    second_pid: Queue[int] = Queue()

    def admin_action():
        admin_client = Client()
        admin_client.force_login(User.objects.get(pk=actor.pk))
        with transaction.atomic():
            toggle(admin_client, ClanMissionDefinition.objects.get(pk=definition.pk))
            if admin_first:
                held.set()
                assert release.wait(15)
        return True

    real_record = missions.record_mission_progress

    def pause_after_record(**kwargs):
        result = real_record(**kwargs)
        if not admin_first:
            held.set()
            assert release.wait(15)
        return result

    monkeypatch.setattr(missions, "record_mission_progress", pause_after_record)
    first = admin_action if admin_first else missions.process_mission_signals
    second = missions.process_mission_signals if admin_first else admin_action
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(_thread, first, first_pid)
        try:
            blocker = first_pid.get(timeout=10)
            assert held.wait(10)
            follower = pool.submit(_thread, second, second_pid)
            _assert_blocked(second_pid.get(timeout=10), blocker)
        finally:
            release.set()
        assert leader.result(timeout=20)
        assert follower.result(timeout=20)
    definition.refresh_from_db()
    assert not definition.enabled
    assert ClanMissionProgress.objects.get().completed_at == interval.starts_at
    assert cs.economic_settlements.count() == 1
    assert missions.process_mission_signals() == 0
    assert _clan_mission_signals(cs.season, timezone.now()).pending_count == 0


def test_source_time_season_and_recovery_guards_survive_deactivation(client, lifecycle):
    from apps.clans.models import ClanSeasonEvent

    _, _, season, cs, member, create, *_ = lifecycle
    definition = create(ends_at=season.ends_at + timedelta(hours=1), target_units=10)
    interval = toggle(client, definition)
    queue(member, "valid-before-recovery", interval.starts_at)
    lost_at = timezone.now()
    ClanSeasonEvent.objects.create(clan_season=cs, kind="BOT_ADMIN_LOST", occurred_at=lost_at)
    queue(member, "during-recovery", lost_at)
    queue(member, "at-season-end", season.ends_at)
    # End-of-Season eligibility is independently rejected by the canonical hook.
    with pytest.raises(missions.MissionSourceIneligible):
        missions.record_mission_progress(
            definition=definition,
            membership=member,
            source_id="closed-boundary-direct",
            units=1,
            occurred_at=season.ends_at,
        )
    toggle(client, definition)
    assert missions.process_mission_signals() == 3
    assert ClanMissionProgress.objects.get().units == 1
    assert list(ClanMissionSourceEvent.objects.values_list("source_id", flat=True)) == [
        "valid-before-recovery"
    ]
    assert not cs.economic_settlements.exists()


def test_finalized_season_barrier_does_not_allow_late_mission_rewards(client, lifecycle):
    from apps.seasons.barrier import SeasonFinalizationEstablished
    from apps.seasons.models import SeasonCloseRun

    _, _, season, cs, member, create, *_ = lifecycle
    definition = create()
    interval = toggle(client, definition)
    queue(member, "late-after-finalization", interval.starts_at)
    toggle(client, definition)
    SeasonCloseRun.objects.create(
        season=season,
        idempotency_key="lifecycle-finalized",
        cutoff_at=timezone.now(),
        service_actor="isolated-test",
        reason="Verify finalization fence",
        barrier_established_at=timezone.now(),
    )
    with pytest.raises(SeasonFinalizationEstablished):
        missions.record_mission_progress(
            definition=definition,
            membership=member,
            source_id="late-direct",
            units=1,
            occurred_at=interval.starts_at,
        )
    assert missions.process_mission_signals() == 0
    assert not ClanMissionProgress.objects.exists() and not cs.economic_settlements.exists()
    signal = ClanMissionSignal.objects.get()
    assert signal.processed_at is None and signal.attempts == 1


def test_actual_migration_cutover_preserves_history_and_holds_uncertain_sources(client, lifecycle):
    from django.db.migrations.executor import MigrationExecutor

    now, config, season, cs, member, create, *_ = lifecycle
    definition = create(target_units=3)
    interval = toggle(client, definition)
    committed_at = interval.starts_at
    missions.record_mission_progress(
        definition=definition,
        membership=member,
        source_id="committed-before-cutover",
        units=1,
        occurred_at=committed_at,
    )
    queue(member, "committed-before-cutover", committed_at)
    assert missions.process_mission_signals() == 1
    processed_at = ClanMissionSignal.objects.get().processed_at
    settled_definition = create(requirement="CLAN_CONTRIBUTION")
    settled_interval = toggle(client, settled_definition)
    settled = missions.record_mission_progress(
        definition=settled_definition,
        membership=member,
        source_id="settled-history",
        units=1,
        occurred_at=settled_interval.starts_at,
    )
    settlement = settled.reward_settlement
    assert settlement is not None
    economic_history = (
        settlement.pk,
        settlement.gross_amount,
        settlement.player_amount,
        settlement.treasury_amount,
        settlement.metadata,
    )
    cs.refresh_from_db()
    balances = (cs.treasury_balance, cs.contribution_total, cs.hq_xp, cs.competitive_score)
    # Real reverse/forward migration simulates a database that predates intervals.
    before = [("clans", "0018_b6_treasury_fractional_carry")]
    after = [("clans", "0019_b6_mission_active_intervals")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    try:
        historical = executor.loader.project_state(before).apps
        OldDefinition = historical.get_model("clans", "ClanMissionDefinition")
        old_disabled = OldDefinition.objects.create(
            key="legacy-disabled",
            title="Legacy observed draft",
            version=1,
            kind="GROUP",
            requirement="VALID_CLAN_ACTIVITY",
            target_units=1,
            reward_band="GROUP",
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(hours=2),
            enabled=False,
            config_version_id=config.pk,
        )
    finally:
        MigrationExecutor(connection).migrate(after)
    definition.refresh_from_db()
    cutover = definition.history_unknown_before
    observation = definition.active_intervals.get()
    assert observation.starts_at == cutover and observation.origin == "CUTOVER_OBSERVATION"
    assert definition.activated_at < cutover
    assert ClanMissionDefinition.objects.get(pk=old_disabled.pk).history_unknown_before == cutover
    assert not ClanMissionActivation.objects.filter(definition_id=old_disabled.pk).exists()
    source = ClanMissionSourceEvent.objects.get(source_id="committed-before-cutover")
    assert source.activation_id is None and source.occurred_at == committed_at
    # Existing committed identity is preserved, never retroactively reassessed/minted.
    assert (
        missions.record_mission_progress(
            definition=definition,
            membership=member,
            source_id=source.source_id,
            units=1,
            occurred_at=committed_at,
        ).units
        == 1
    )
    queue(member, "uncertain-before-cutover", committed_at)
    assert missions.process_mission_signals() == 0
    held = ClanMissionSignal.objects.get(source_key="uncertain-before-cutover")
    assert held.requires_review and held.error_category == "activation_history_unknown"
    assert held.attempts == 0 and held.processed_at is None
    assert missions.process_mission_signals() == 0
    assert ClanMissionProgress.objects.get(definition=definition).units == 1
    assert ClanMissionSourceEvent.objects.count() == 2
    assert cs.economic_settlements.count() == 1
    settlement.refresh_from_db()
    assert (
        settlement.pk,
        settlement.gross_amount,
        settlement.player_amount,
        settlement.treasury_amount,
        settlement.metadata,
    ) == economic_history
    cs.refresh_from_db()
    assert (cs.treasury_balance, cs.contribution_total, cs.hq_xp, cs.competitive_score) == balances
    assert (
        ClanMissionSignal.objects.get(source_key="committed-before-cutover").processed_at
        == processed_at
    )
    evidence = _clan_mission_signals(season, timezone.now())
    assert evidence.pending_count == 1 and evidence.blocking
    # Prospective eligibility starts at the observed cutover, inclusive.
    queue(member, "at-observed-cutover", cutover)
    assert missions.process_mission_signals() == 1
    assert ClanMissionProgress.objects.get(definition=definition).units == 2
    assert (
        ClanMissionSourceEvent.objects.get(source_id="at-observed-cutover").activation_id
        == observation.pk
    )
    assert held.__class__.objects.get(pk=held.pk).processed_at is None
    with pytest.raises(DatabaseError), transaction.atomic():
        ClanMissionDefinition.objects.filter(pk=definition.pk).update(history_unknown_before=None)
    with pytest.raises(DatabaseError), transaction.atomic():
        create(history_unknown_before=now)
    # Activation of an observed disabled legacy definition cannot authorize its
    # uncertain earlier signals. The separate requirement isolates this hold.
    disabled = ClanMissionDefinition.objects.get(pk=old_disabled.pk)
    missions.queue_mission_signal(
        membership=member,
        source_key="disabled-legacy-history",
        requirement="VALID_CLAN_ACTIVITY",
        units=1,
        occurred_at=now,
    )
    toggle(client, disabled)
    assert missions.process_mission_signals() == 0
    assert ClanMissionSignal.objects.get(source_key="disabled-legacy-history").requires_review
    assert not ClanMissionProgress.objects.filter(definition=disabled).exists()
    assert _clan_mission_signals(season, timezone.now()).pending_count == 2


def test_published_config_identity_still_controls_source_eligibility(
    client, lifecycle, phase4_config_factory
):
    now, _, _, cs, member, create, *_ = lifecycle
    other = phase4_config_factory(
        effective_at=now + timedelta(days=1), version="lifecycle-other-published"
    )
    definition = create(config_version=other)
    interval = toggle(client, definition)
    queue(member, "wrong-pinned-config", interval.starts_at)
    assert missions.process_mission_signals() == 1
    assert not ClanMissionProgress.objects.exists() and not cs.economic_settlements.exists()
    with pytest.raises(missions.MissionSourceIneligible):
        missions.record_mission_progress(
            definition=definition,
            membership=member,
            source_id="wrong-config-direct",
            units=1,
            occurred_at=interval.starts_at,
        )
