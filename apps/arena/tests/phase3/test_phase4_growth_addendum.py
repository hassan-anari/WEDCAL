# ruff: noqa: F811
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connections

from apps.arena.automation import run_weekly_lifecycle
from apps.arena.economics import prepare_epoch
from apps.arena.epochs import begin_draining, freeze_epoch, reconcile_epoch
from apps.arena.growth import unlock_growth
from apps.arena.models import GrowthEpochFunding, GrowthEvaluation, GrowthUnlockEvent
from apps.arena.readiness import evaluate_epoch_readiness

from .test_phase4_configurable import configurable_world, milestone  # noqa: F401


def qualify(w, count, epoch):
    players = [w.player() for _ in range(count)]
    for p in players:
        for day in (0, 1):
            w.typing(p, epoch.starts_at + timedelta(days=day, hours=1))
    return players


def demo(configurable_world):
    return configurable_world(
        base=3000000, maximum=5000000, milestones=[milestone("demo-qualified-20", 20, 2000000)]
    )


def write_growth_report(w, name):
    report = {
        "synthetic": True,
        "base_budget": 3000000,
        "maximum_budget": 5000000,
        "events": [
            {
                "id": event.pk,
                "milestone": event.milestone_key,
                "qualified_count": event.qualified_player_count,
                "authorized_amount": event.unlocked_amount,
                "provenance": event.evidence,
                "receivers": list(
                    event.funding.order_by("epoch__epoch_number").values(
                        "epoch__epoch_number", "amount"
                    )
                ),
            }
            for event in w.season.growth_unlocks.order_by("pk")
        ],
        "evaluations": list(
            GrowthEvaluation.objects.filter(season=w.season)
            .order_by("pk")
            .values("outcome", "qualified_player_count", "cutoff_at", "cohort_digest")
        ),
    }
    directory = Path("work/phase4-growth-reports")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(report, indent=2, default=str) + "\n")


@pytest.mark.django_db
@pytest.mark.parametrize("population", [19, 20, 21])
def test_demo_first_cutoff_exact_threshold_and_funding(configurable_world, population):  # noqa: F811
    w = demo(configurable_world)
    qualify(w, population, w.epochs[0])
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at + timedelta(
        minutes=3
    )  # Delayed worker does not lose Epoch 1.
    assert run_weekly_lifecycle(w.season) == 1
    if population < 20:
        assert not GrowthUnlockEvent.objects.exists()
        assert GrowthEvaluation.objects.get().qualified_player_count == 19
        assert not GrowthEpochFunding.objects.exists()
    else:
        event = GrowthUnlockEvent.objects.get()
        assert event.qualified_player_count == population
        assert event.unlocked_amount == 2000000
        assert list(
            event.funding.order_by("epoch__epoch_number").values_list("amount", flat=True)
        ) == [1000000, 1000000]
        assert event.evidence["qualifying_cutoff"] == w.epochs[0].ends_at.isoformat()
    assert all(reconcile_epoch(w.epochs[0]).values())
    write_growth_report(w, f"first-cutoff-{population}")


@pytest.mark.django_db
def test_demo_final_cutoff_counts_distinct_prior_and_current_participants(configurable_world):  # noqa: F811
    w = demo(configurable_world)
    prior = qualify(w, 10, w.epochs[0])
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    run_weekly_lifecycle(w.season)
    assert not GrowthUnlockEvent.objects.exists()
    qualify(w, 10, w.epochs[1])
    # Earlier legitimately qualified players remain participants even when inactive this week.
    w.clock["at"] = w.epochs[1].ends_at + timedelta(hours=2)
    run_weekly_lifecycle(w.season)
    event = GrowthUnlockEvent.objects.get()
    assert event.qualified_player_count == 20
    assert event.unlocked_amount == 2000000
    assert list(event.funding.values_list("epoch_id", "amount")) == [(w.epochs[1].pk, 2000000)]
    assert len(w.epochs) == 2 and len(prior) == 10
    assert all(all(reconcile_epoch(epoch).values()) for epoch in w.epochs)
    write_growth_report(w, "final-cutoff")


@pytest.mark.django_db
def test_no_receiver_is_durable_unfunded_and_idempotent(configurable_world):  # noqa: F811
    w = demo(configurable_world)
    players = qualify(w, 20, w.epochs[0])
    w.enable()
    # Simulate existing immutable history created before the new pre-Freeze coordinator.
    with patch("apps.arena.growth.unlock_growth", return_value=[]):
        w.clock["at"] = w.epochs[0].ends_at
        run_weekly_lifecycle(w.season)
        for p in players:
            for day in (0, 1):
                w.typing(p, w.epochs[1].starts_at + timedelta(days=day, hours=1))
        w.clock["at"] = w.epochs[1].ends_at
        run_weekly_lifecycle(w.season)
    unlock_growth(w.season)
    unlock_growth(w.season)
    event = GrowthUnlockEvent.objects.get()
    assert event.unlocked_amount == 0
    assert event.evidence["outcome"] == "NO_ELIGIBLE_MUTABLE_EPOCH"
    assert event.evidence["requested_amount"] == 2000000
    assert not GrowthEpochFunding.objects.exists()
    assert GrowthEvaluation.objects.count() == 2
    write_growth_report(w, "no-mutable-receiver")


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["FLAGGED", "BANNED"])
def test_later_pending_excludes_future_count_without_rewriting_prior_events(
    configurable_world, state
):  # noqa: F811
    w = configurable_world(
        count=3, milestones=[milestone("first", 10, 101), milestone("second", 20, 203)]
    )
    prior = qualify(w, 10, w.epochs[0])
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    run_weekly_lifecycle(w.season)
    before = GrowthUnlockEvent.objects.get()
    # A new review affects only the next cutoff; no past event is rewritten.
    prior[0].moderation_state = state
    prior[0].save(update_fields=["moderation_state"])
    qualify(w, 10, w.epochs[1])
    # Requalification of earlier participants does not increase the distinct Season counter.
    for p in prior[1:3]:
        for day in (0, 1):
            w.typing(p, w.epochs[1].starts_at + timedelta(days=day, hours=1))
    w.clock["at"] = w.epochs[1].ends_at
    run_weekly_lifecycle(w.season)
    assert GrowthUnlockEvent.objects.count() == 1
    before.refresh_from_db()
    assert before.qualified_player_count == 10 and before.unlocked_amount == 101
    latest = GrowthEvaluation.objects.order_by("-pk").first()
    assert latest is not None and latest.qualified_player_count == 19


@pytest.mark.django_db
def test_crash_between_growth_authorization_and_projection_rolls_back(
    configurable_world, monkeypatch
):  # noqa: F811
    from apps.arena.models import EconomicEpoch

    w = demo(configurable_world)
    qualify(w, 20, w.epochs[0])
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    original = EconomicEpoch.save

    def interrupt(self, *args, **kwargs):
        if kwargs.get("update_fields") == ("growth_budget",):
            raise ConnectionError("synthetic interruption after funding insertion")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EconomicEpoch, "save", interrupt)
    with pytest.raises(ConnectionError):
        run_weekly_lifecycle(w.season)
    assert not GrowthUnlockEvent.objects.exists() and not GrowthEpochFunding.objects.exists()
    w.epochs[0].refresh_from_db()
    assert w.epochs[0].growth_budget == 0 and w.epochs[0].state != "FROZEN"
    monkeypatch.setattr(EconomicEpoch, "save", original)
    assert run_weekly_lifecycle(w.season) == 1
    assert GrowthUnlockEvent.objects.get().unlocked_amount == 2000000


@pytest.mark.django_db(transaction=True)
def test_growth_and_freeze_share_exclusive_transaction_barrier(configurable_world):  # noqa: F811
    w = demo(configurable_world)
    qualify(w, 20, w.epochs[0])
    w.clock["at"] = w.epochs[0].ends_at
    epoch = begin_draining(w.epochs[0], at=w.clock["at"])
    prepare_epoch(epoch)
    assert (
        evaluate_epoch_readiness(epoch, evaluation_key="before-growth-freeze").status == "COMPLETE"
    )
    barrier = Barrier(2)

    def worker(kind):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if kind == "growth":
                return unlock_growth(w.season, epoch=epoch)
            return freeze_epoch(epoch)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, ["growth", "freeze"]))
    assert GrowthUnlockEvent.objects.count() == 1
    assert sum(GrowthEpochFunding.objects.values_list("amount", flat=True)) == 2000000
    epoch.refresh_from_db()
    assert epoch.state == "FROZEN" and epoch.growth_budget == 1000000


@pytest.mark.django_db
@pytest.mark.parametrize("count,closing,gate", [(2, 1, 1), (6, 3, 3)])
@pytest.mark.parametrize("recognize_at_final", [False, True])
def test_growth_full_close_conservation_in_short_and_official_topology(
    configurable_world, count, closing, gate, recognize_at_final, monkeypatch
):
    from apps.arena.corrections import finalize_correction_round
    from apps.arena.policy import CATEGORIES
    from apps.arena.services import consume_pending_events
    from apps.rewards.models import RewardSettlementOutbox
    from apps.rewards.tasks import consume_outbox
    from apps.seasons.close import advance_close, reconcile_season_dlx, start_or_resume_close

    w = configurable_world(
        count=count,
        closing=closing,
        gate=gate,
        base=3000000,
        maximum=5000000,
        milestones=[milestone("configured-growth", 1, 2000000)],
    )
    w.enable()
    origin = w.epochs[-1] if recognize_at_final else w.epochs[0]
    qualify(w, 1, origin)
    for epoch in w.epochs:
        w.clock["at"] = epoch.ends_at
        run_weekly_lifecycle(w.season)
    event = GrowthUnlockEvent.objects.get()
    assert event.unlocked_amount == 2000000
    if recognize_at_final:
        assert list(event.funding.values_list("epoch_id", "amount")) == [(origin.pk, 2000000)]
    else:
        assert event.funding.count() == count
    for epoch in w.epochs:
        epoch.refresh_from_db()
        assert epoch.state == "SETTLED"
        assert all(reconcile_epoch(epoch).values())
    w.clock["at"] = w.season.ends_at
    for epoch in w.epochs:
        for category in CATEGORIES:
            finalize_correction_round(epoch, category, actor=w.actor)
    result = reconcile_season_dlx(w.season, record_residuals=True)
    assert result["reconciled"], result
    assert (
        result["confirmed_allocations"] + result["funded_corrections"] + result["final_residual"]
        == 5000000
    )
    monkeypatch.setattr("apps.seasons.close._enqueue", lambda *_a, **_kw: None)
    run = start_or_resume_close(w.season, actor=w.actor, reason="synthetic Growth close proof")
    for _ in range(20):
        for pk in RewardSettlementOutbox.objects.exclude(state="PROCESSED").values_list(
            "pk", flat=True
        ):
            consume_outbox(pk)
        consume_pending_events()
        if advance_close(run.pk) == "COMPLETED":
            break
    w.season.refresh_from_db()
    assert w.season.status == "CLOSED"
