from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.arena.adjustments import record_adjustment
from apps.arena.epochs import (
    drain_blockers,
    freeze_epoch,
    initialize_epochs,
    reconcile_epoch,
    reconcile_player_balance,
    settle_epoch,
    snapshot_player_input,
)
from apps.arena.models import (
    DlxAdjustment,
    DlxAllocation,
    EconomicEpoch,
    EpochCategoryBudget,
    EpochPerformanceSnapshot,
    QualificationDecision,
)
from apps.arena.qualification import (
    is_player_qualified,
    qualified_player_ids_at,
    record_qualification_decision,
)
from apps.moderation.models import ModerationAction, ModerationCase
from apps.players.models import Player
from apps.seasons.services import set_settlement_start_epoch
from apps.typing_game.models import TypingWallet

AT = datetime(2026, 1, 2, tzinfo=UTC)


def qualify(player: Player, config_id: int, source_id: str = "qualified") -> QualificationDecision:
    row, _ = record_qualification_decision(
        player=player,
        policy_key="QUALIFIED_PLAYER",
        policy_version=1,
        decision="QUALIFIED",
        effective_at=AT,
        reason_code="REVIEWED",
        source="TEST",
        source_id=source_id,
        evidence={"ticket": source_id},
    )
    return row


def settled_allocation(config, season, player) -> DlxAllocation:  # type: ignore[no-untyped-def]
    season.settlement_start_epoch = 1
    season.save(update_fields=("settlement_start_epoch",))
    epoch = EconomicEpoch.objects.create(
        season=season,
        epoch_number=1,
        starts_at=season.starts_at,
        ends_at=season.starts_at + timedelta(days=7),
        state="DRAINING",
        config_version=config,
        base_budget=100,
    )
    decision = qualify(player, config.pk, f"allocation-{player.pk}")
    snapshot = EpochPerformanceSnapshot.objects.create(
        epoch=epoch,
        category="CORE_GAMEPLAY",
        player=player,
        raw_inputs={"points": 10},
        performance_units=0,
        qualification_decision=decision,
        config_version=config,
    )
    EpochCategoryBudget.objects.create(
        epoch=epoch,
        category="CORE_GAMEPLAY",
        weight_bps=10000,
        base_epoch_budget=100,
        effective_budget=100,
        carry_materialized=True,
        config_version=config,
    )
    epoch.state = "FROZEN"
    epoch.frozen_at = AT
    epoch.snapshots_completed_at = AT
    epoch.save(update_fields=("state", "frozen_at", "snapshots_completed_at"))
    settle_epoch(epoch, strategies={"CORE_GAMEPLAY": lambda _budget, _rows: {player.pk: 60}}, at=AT)
    return DlxAllocation.objects.get(snapshot=snapshot)


@pytest.mark.django_db
@pytest.mark.parametrize("later_decision", ["DISQUALIFIED", "PENDING"])
def test_latest_qualification_decision_and_policy_version(  # type: ignore[no-untyped-def]
    phase4_config_factory, player_factory, later_decision: str
) -> None:
    config = phase4_config_factory()
    player = player_factory()
    qualify(player, config.pk)
    record_qualification_decision(
        player=player,
        policy_key="QUALIFIED_PLAYER",
        policy_version=1,
        decision=later_decision,
        effective_at=AT + timedelta(hours=1),
        reason_code="REVIEW",
        source="TEST",
        source_id="disqualified",
    )
    assert is_player_qualified(player, policy_key="QUALIFIED_PLAYER", policy_version=1, at=AT)
    assert not is_player_qualified(
        player,
        policy_key="QUALIFIED_PLAYER",
        policy_version=1,
        at=AT + timedelta(hours=2),
    )
    assert player.pk not in qualified_player_ids_at(
        policy_key="QUALIFIED_PLAYER", policy_version=1, at=AT + timedelta(hours=2)
    )
    assert not is_player_qualified(player, policy_key="QUALIFIED_PLAYER", policy_version=2, at=AT)


@pytest.mark.django_db
@pytest.mark.parametrize(("action_offset", "qualified_at_cutoff"), [(-1, False), (1, True)])
def test_source_time_moderation_does_not_reinterpret_cutoff(  # type: ignore[no-untyped-def]
    phase4_config_factory, player_factory, action_offset: int, qualified_at_cutoff: bool
) -> None:
    config = phase4_config_factory()
    player, reporter = player_factory(9101), player_factory(9102)
    qualify(player, config.pk)
    case = ModerationCase.objects.create(
        reporter=reporter,
        target=player,
        category="FRAUD",
        reason="evidence",
        config_version=config,
    )
    action = ModerationAction.objects.create(
        case=case,
        system_actor="TEST",
        target=player,
        action_type="SUSPEND",
        reason="review",
        previous_state={"moderation_state": "ACTIVE"},
        resulting_state={"moderation_state": "SUSPENDED"},
        idempotency_key="suspend",
    )
    ModerationAction.objects.filter(pk=action.pk).update(
        created_at=AT + timedelta(hours=action_offset)
    )
    assert (
        is_player_qualified(player, policy_key="QUALIFIED_PLAYER", policy_version=1, at=AT)
        is qualified_at_cutoff
    )
    assert not is_player_qualified(
        player,
        policy_key="QUALIFIED_PLAYER",
        policy_version=1,
        at=AT + timedelta(hours=2),
    )


@pytest.mark.django_db(transaction=True)
def test_qualification_and_frozen_snapshot_are_database_immutable(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    decision = qualify(player, config.pk)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE arena_qualificationdecision SET reason_code='x' WHERE id=%s", [decision.pk]
        )
    epoch = EconomicEpoch.objects.create(
        season=season,
        epoch_number=1,
        starts_at=season.starts_at,
        ends_at=season.starts_at + timedelta(days=7),
        state="DRAINING",
        config_version=config,
        base_budget=100,
    )
    snapshot = EpochPerformanceSnapshot.objects.create(
        epoch=epoch,
        category="CORE_GAMEPLAY",
        player=player,
        raw_inputs={},
        qualification_decision=decision,
        config_version=config,
    )
    epoch.state = "FROZEN"
    epoch.save(update_fields=("state",))
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM arena_epochperformancesnapshot WHERE id=%s", [snapshot.pk])
    with pytest.raises(DatabaseError), transaction.atomic():
        EpochPerformanceSnapshot.objects.create(
            epoch=epoch,
            category="LOYALTY_CONSISTENCY",
            player=player,
            raw_inputs={},
            qualification_decision=decision,
            config_version=config,
        )


@pytest.mark.django_db
def test_actual_ledger_reconciliation_detects_projection_drift(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    allocation = settled_allocation(config, season, player)
    epoch = allocation.epoch
    assert all(reconcile_epoch(epoch).values())
    EpochCategoryBudget.objects.filter(epoch=epoch).update(allocated_amount=59)
    assert not all(reconcile_epoch(epoch).values())
    projection = {"confirmed": 60, "held": 0, "forfeited": 0, "net_confirmed": 60, "claimable": 0}
    assert reconcile_player_balance(player, season, projection)
    projection["confirmed"] = 59
    assert not reconcile_player_balance(player, season, projection)


@pytest.mark.django_db
def test_delayed_same_category_carry_materializes_only_after_prior_settlement(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    season.settlement_start_epoch = 3
    season.save(update_fields=("settlement_start_epoch",))
    decision = qualify(player, config.pk, "carry-player")
    epochs = []
    for number in (1, 2, 3):
        start = season.starts_at + timedelta(days=7 * (number - 1))
        epoch = EconomicEpoch.objects.create(
            season=season,
            epoch_number=number,
            starts_at=start,
            ends_at=start + timedelta(days=7),
            state="DRAINING",
            config_version=config,
            base_budget=100,
        )
        EpochPerformanceSnapshot.objects.create(
            epoch=epoch,
            category="CORE_GAMEPLAY",
            player=player,
            raw_inputs={"epoch": number},
            qualification_decision=decision,
            config_version=config,
        )
        EpochCategoryBudget.objects.create(
            epoch=epoch,
            category="CORE_GAMEPLAY",
            weight_bps=10000,
            base_epoch_budget=100,
            effective_budget=100,
            carry_materialized=number == 1,
            config_version=config,
        )
        epoch.state = "FROZEN"
        epoch.frozen_at = epoch.ends_at
        epoch.snapshots_completed_at = epoch.ends_at
        epoch.save(update_fields=("state", "frozen_at", "snapshots_completed_at"))
        epochs.append(epoch)
    assert not EpochCategoryBudget.objects.get(epoch=epochs[1]).carry_materialized
    gate_at = season.starts_at + timedelta(days=14)
    settle_epoch(
        epochs[0], strategies={"CORE_GAMEPLAY": lambda _b, _s: {player.pk: 60}}, at=gate_at
    )
    settle_epoch(epochs[1], strategies={"CORE_GAMEPLAY": lambda _b, _s: {}}, at=gate_at)
    second = EpochCategoryBudget.objects.get(epoch=epochs[1])
    assert (second.carry_in, second.effective_budget, second.carry_out) == (40, 140, 140)
    settle_epoch(epochs[2], strategies={"CORE_GAMEPLAY": lambda _b, _s: {}}, at=gate_at)
    third = EpochCategoryBudget.objects.get(epoch=epochs[2])
    assert (third.carry_in, third.effective_budget, third.carry_out) == (140, 240, 240)


@pytest.mark.django_db(transaction=True)
def test_adjustment_idempotency_contention_and_append_only(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    allocation = settled_allocation(config, season, player)
    first, created = record_adjustment(
        allocation=allocation,
        kind="HOLD",
        amount=20,
        reason_code="REVIEW",
        idempotency_key="hold",
        system_actor="fraud-engine",
        evidence_reference="case:1",
        metadata={"source": "rule"},
    )
    duplicate, created_again = record_adjustment(
        allocation=allocation,
        kind="HOLD",
        amount=20,
        reason_code="REVIEW",
        idempotency_key="hold",
        system_actor="fraud-engine",
        evidence_reference="case:1",
        metadata={"source": "rule"},
    )
    assert created and not created_again and duplicate.pk == first.pk
    with pytest.raises(ValueError, match="idempotency conflict"):
        record_adjustment(
            allocation=allocation,
            kind="HOLD",
            amount=20,
            reason_code="REVIEW",
            idempotency_key="hold",
            system_actor="other",
            evidence_reference="case:2",
            metadata={"source": "other"},
        )
    record_adjustment(
        allocation=allocation,
        kind="RELEASE_HOLD",
        amount=5,
        reason_code="CLEARED",
        idempotency_key="release",
        system_actor="review",
    )
    record_adjustment(
        allocation=allocation,
        kind="FORFEIT",
        amount=30,
        reason_code="PROVEN",
        idempotency_key="forfeit",
        system_actor="review",
    )
    record_adjustment(
        allocation=allocation,
        kind="REVERSAL",
        amount=10,
        reason_code="APPEAL",
        idempotency_key="appeal",
        system_actor="review",
    )
    with pytest.raises(ValueError, match="exceeds allocation"):
        record_adjustment(
            allocation=allocation,
            kind="FORFEIT",
            amount=50,
            reason_code="TOO_MUCH",
            idempotency_key="over",
            system_actor="review",
        )
    for table, row_id in (
        ("arena_dlxallocation", allocation.pk),
        ("arena_dlxadjustment", first.pk),
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET id=id WHERE id=%s", [row_id])
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE id=%s", [row_id])


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_forfeit_converges_once(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    allocation = settled_allocation(config, season, player)

    def apply() -> int:
        close_old_connections()
        try:
            row, _ = record_adjustment(
                allocation=DlxAllocation.objects.get(pk=allocation.pk),
                kind="FORFEIT",
                amount=20,
                reason_code="PROVEN",
                idempotency_key="same",
                system_actor="review",
            )
            return row.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = [future.result(timeout=10) for future in (pool.submit(apply), pool.submit(apply))]
    assert ids[0] == ids[1]
    assert DlxAdjustment.objects.filter(idempotency_key="same").count() == 1


@pytest.mark.django_db(transaction=True)
def test_hold_and_forfeit_contention_cannot_over_adjust(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    allocation = settled_allocation(config, season, player)

    def apply(kind: str) -> str:
        close_old_connections()
        try:
            record_adjustment(
                allocation=DlxAllocation.objects.get(pk=allocation.pk),
                kind=kind,
                amount=40,
                reason_code="RACE",
                idempotency_key=f"race:{kind}",
                system_actor="review",
            )
            return "CREATED"
        except ValueError:
            return "REJECTED"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(apply, "HOLD"), pool.submit(apply, "FORFEIT"))
        results = sorted(future.result(timeout=10) for future in futures)
    assert results == ["CREATED", "REJECTED"]
    assert DlxAdjustment.objects.filter(allocation=allocation).count() == 1


@pytest.mark.django_db
def test_freeze_builds_snapshots_and_unclaimed_typing_is_not_a_blocker(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    qualify(player, config.pk)
    TypingWallet.objects.create(player=player, season=season, claimable_balance=50)
    epoch = initialize_epochs(season)[0]
    assert all(blocker.provider != "TYPING_WALLET" for blocker in drain_blockers(epoch))
    frozen = freeze_epoch(epoch, at=epoch.ends_at)
    assert frozen.state == EconomicEpoch.State.FROZEN
    assert frozen.snapshots_completed_at == epoch.ends_at
    assert set(frozen.performance_snapshots.values_list("category", flat=True)) == {
        "CLAN_PERFORMANCE",
        "CORE_GAMEPLAY",
        "CREATOR_COMMUNITY_GROWTH",
        "VERIFIED_REFERRAL",
        "LOYALTY_CONSISTENCY",
    }
    with pytest.raises(ValidationError):
        snapshot_player_input(
            epoch=frozen,
            category="CORE_GAMEPLAY",
            player=player,
            raw_inputs={},
            performance_units=0,
        )


@pytest.mark.django_db
def test_settlement_control_is_closed_after_first_real_transition(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    settled_allocation(config, season, player)
    season.refresh_from_db()
    assert season.settlement_started_at is not None
    with pytest.raises(ValidationError):
        set_settlement_start_epoch(season, 2)


@pytest.mark.django_db(transaction=True)
def test_first_settlement_and_control_change_have_one_serialized_winner(  # type: ignore[no-untyped-def]
    phase4_config_factory, season_factory, player_factory
) -> None:
    config = phase4_config_factory()
    season = season_factory(config)
    player = player_factory()
    season.settlement_start_epoch = 1
    season.save(update_fields=("settlement_start_epoch",))
    epoch = EconomicEpoch.objects.create(
        season=season,
        epoch_number=1,
        starts_at=season.starts_at,
        ends_at=season.starts_at + timedelta(days=7),
        state="DRAINING",
        config_version=config,
        base_budget=100,
    )
    decision = qualify(player, config.pk, "control-race")
    EpochPerformanceSnapshot.objects.create(
        epoch=epoch,
        category="CORE_GAMEPLAY",
        player=player,
        raw_inputs={},
        qualification_decision=decision,
        config_version=config,
    )
    EpochCategoryBudget.objects.create(
        epoch=epoch,
        category="CORE_GAMEPLAY",
        weight_bps=10000,
        base_epoch_budget=100,
        effective_budget=100,
        carry_materialized=True,
        config_version=config,
    )
    epoch.state = "FROZEN"
    epoch.save(update_fields=("state",))

    def settle() -> str:
        close_old_connections()
        try:
            settle_epoch(
                EconomicEpoch.objects.get(pk=epoch.pk),
                strategies={"CORE_GAMEPLAY": lambda _b, _s: {}},
                at=AT,
            )
            return "SETTLED"
        except Exception as exc:
            return type(exc).__name__
        finally:
            close_old_connections()

    def change() -> str:
        close_old_connections()
        try:
            set_settlement_start_epoch(Season.objects.get(pk=season.pk), 2)
            return "CHANGED"
        except Exception as exc:
            return type(exc).__name__
        finally:
            close_old_connections()

    from apps.seasons.models import Season

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(settle), pool.submit(change))
        results = {future.result(timeout=10) for future in futures}
    season.refresh_from_db()
    epoch.refresh_from_db()
    assert results in ({"SETTLED", "ValidationError"}, {"CHANGED", "SettlementOrderBlocked"})
    assert (season.settlement_started_at is not None) == (epoch.state == "SETTLED")
