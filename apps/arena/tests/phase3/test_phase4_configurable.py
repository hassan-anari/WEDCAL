# mypy: disable-error-code="no-untyped-def,no-untyped-call"
"""Phase 4 profiles exercise the real PostgreSQL services, without Production authority."""

import copy
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction

from apps.arena.automation import control_automation, run_weekly_lifecycle
from apps.arena.corrections import finalize_correction_round
from apps.arena.epochs import initialize_epochs, reconcile_epoch
from apps.arena.growth import unlock_growth
from apps.arena.models import (
    CategoryCorrectionReserve,
    DlxAllocation,
    EconomicAutomationAttempt,
    EconomicPolicyVersion,
    GrowthEpochFunding,
    GrowthUnlockEvent,
)
from apps.arena.policy import CATEGORIES, digest
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import (
    parse_economy_sync,
    publish_game_config,
    resolve_game_config_version,
)
from apps.seasons.close import reconcile_season_dlx
from apps.seasons.models import Season
from apps.seasons.services import activate_season, economic_window_end
from apps.typing_game.models import TypingPhrase
from apps.typing_game.services import start_attempt, submit_attempt

from .conftest import POLICY, START, TEXT

V2 = json.loads(
    (
        Path(__file__).resolve().parents[4] / "config/examples/season1.game-config.v2.json"
    ).read_text()
)


@pytest.fixture
def configurable_world(phase4_config_factory, player_factory, clock, monkeypatch):
    monkeypatch.setattr("apps.rewards.services.dispatch_reward_outbox_safely", lambda _pk: None)
    monkeypatch.setattr("apps.arena.services.dispatch_projection_safely", lambda _pk: None)
    sequence = 0

    def make(
        count=2,
        days=7,
        closing=1,
        gate=1,
        base=100003,
        maximum=200019,
        milestones=(),
        targets=None,
        registered=False,
    ):
        nonlocal sequence
        sequence += 1
        actor = User.objects.create_superuser(username=f"phase4-owner-{sequence}", password="test")
        values = copy.deepcopy(POLICY)
        values.update(implementation="CONFIGURABLE_V1", days=days)
        values.pop("growth_milestones")
        if targets is not None:
            values["participation_targets"] = dict.fromkeys(CATEGORIES, targets)
        values["growth"] = {
            "rule": "SEASON_TO_DATE_PRE_FREEZE_EQUAL_V1" if milestones else "DISABLED",
            "approval_reference": "SYNTHETIC_TEST_OWNER_APPROVAL",
        }
        if registered:
            from apps.arena.policy_registration import approve_policy, propose_policy

            author = User.objects.create_superuser(username=f"phase4-policy-author-{sequence}")
            proposal = propose_policy(
                key=f"phase4-{sequence}",
                version=1,
                payload=values,
                actor=author,
                submission_reference="SYNTHETIC_TEST_PROPOSAL",
                expected_checksum=digest(values),
                dry_run=False,
            )
            approval = approve_policy(
                proposal["proposal_id"],
                actor=actor,
                approval_reference="SYNTHETIC_TEST_POLICY_APPROVAL",
                expected_checksum=digest(values),
                dry_run=False,
            )
            document = EconomicPolicyVersion.objects.get(pk=approval["policy_id"])
        else:
            document = EconomicPolicyVersion.objects.create(
                key=f"phase4-{sequence}",
                version=1,
                payload=values,
                checksum=digest(values),
                approval_reference="SYNTHETIC_TEST_POLICY_APPROVAL",
            )

        def configure(payload):
            payload.clear()
            payload.update(copy.deepcopy(V2))
            payload["meta"].update(schema_version=4, economy_contract_version="4.0.0")
            d = payload["dlx_distribution"]
            d["timing"].update(
                product_period_days=count * days + closing,
                epoch_count=count,
                epoch_days=days,
                closing_days=closing,
            )
            d["default_settlement_start_epoch"] = gate
            d["pool"].update(base_dlx_amount=base, max_dlx_amount=maximum)
            d["pool"]["growth_unlock"]["milestones"] = list(milestones)
            d["economic_policy"] = {
                "key": document.key,
                "version": document.version,
                "checksum": document.checksum,
            }

        config = phase4_config_factory(effective_at=START - timedelta(days=3), mutate=configure)
        season = Season.objects.create(
            code=f"configurable-{sequence}",
            title="Disposable test",
            starts_at=START,
            ends_at=START + timedelta(days=count * days + closing),
            baseline_config=config,
        )
        season = activate_season(
            season, actor=actor, evidence_reference="SYNTHETIC_SOURCE_COVERAGE"
        )
        epochs = initialize_epochs(season)
        TypingPhrase.objects.get_or_create(
            key="p4", version=1, defaults={"text": TEXT, "enabled": True}
        )
        player_sequence = 0

        def player():
            nonlocal player_sequence
            player_sequence += 1
            p = player_factory(940000 + player_sequence)
            p.first_mini_app_at = START - timedelta(days=2)
            p.save(update_fields=["first_mini_app_at"])
            return p

        def typing(p, at):
            clock["at"] = at
            attempt, _ = start_attempt(p, f"p4:{p.pk}:{at}", at)
            clock["at"] = at + timedelta(seconds=5)
            return submit_attempt(p, attempt.public_id, TEXT, clock["at"])[0]

        def enable():
            return control_automation(
                season,
                enabled=True,
                actor=actor,
                reason="isolated test",
                evidence_reference="SYNTHETIC_COMPATIBLE_PRODUCERS",
            )

        return SimpleNamespace(
            config=config,
            document=document,
            season=season,
            epochs=epochs,
            resolved=resolve_game_config_version(config.pk),
            actor=actor,
            player=player,
            typing=typing,
            clock=clock,
            enable=enable,
        )

    return make


def milestone(key, threshold, amount):
    return {"key": key, "qualified_player_count": threshold, "unlock_dlx_amount": amount}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "count,days,closing,gate,base,maximum,registered",
    [
        (2, 7, 1, 1, 100003, 200019, False),
        (6, 7, 3, 3, 100003, 200019, False),
        (3, 5, 2, 1, 100003, 200019, False),
        (8, 3, 2, 1, 100003, 200019, False),
        (2, 7, 1, 1, 3000000, 5000000, True),
        (6, 7, 3, 3, 60000000, 100000000, True),
    ],
)
def test_profiles_automatic_full_lifecycle_exact_residual(
    configurable_world, count, days, closing, gate, base, maximum, registered, monkeypatch
):
    w = configurable_world(
        count, days, closing, gate, base=base, maximum=maximum, registered=registered
    )
    p = w.player()
    assert len(w.epochs) == count
    assert economic_window_end(w.season) == w.epochs[-1].ends_at
    assert w.season.ends_at - w.epochs[-1].ends_at == timedelta(days=closing)
    assert sum(e.base_budget for e in w.epochs) == base
    assert run_weekly_lifecycle(w.season) == 0  # Binding alone never activates workers.
    w.enable()
    for epoch in w.epochs:
        for day in (0, 1):
            w.typing(p, epoch.starts_at + timedelta(days=day, hours=1))
        w.clock["at"] = epoch.ends_at
        run_weekly_lifecycle(w.season)
    w.clock["at"] = w.season.ends_at
    run_weekly_lifecycle(w.season)
    for epoch in w.epochs:
        epoch.refresh_from_db()
        assert epoch.state == "SETTLED"
        assert all(reconcile_epoch(epoch).values())
        for category in CATEGORIES:
            finalize_correction_round(epoch, category, actor=w.actor)
    result = reconcile_season_dlx(w.season, record_residuals=True)
    assert result["reconciled"], result
    assert result["confirmed_allocations"] + result["final_residual"] == base
    if registered:
        from apps.rewards.bootstrap import build_bootstrap
        from apps.rewards.schemas import BootstrapOutput

        bootstrap = BootstrapOutput(
            **build_bootstrap(p, w.epochs[-1].ends_at, request_id="phase4-configurable-contract")
        ).model_dump(mode="json")
        timing = bootstrap["season"]["timing"]
        assert timing["epoch_count"] == count and timing["closing_days"] == closing
        assert timing["product_period_days"] == count * days + closing
        assert timing["settlement_start_epoch"] == gate
        assert not bootstrap["gameplay"]["reward_earning_allowed"]
        contract_dir = Path("work/phase4-bootstrap")
        contract_dir.mkdir(parents=True, exist_ok=True)
        (contract_dir / f"registered-{count}.json").write_text(
            json.dumps(bootstrap, indent=2) + "\n"
        )
    before = list(DlxAllocation.objects.values_list("pk", "amount"))
    run_weekly_lifecycle(w.season)
    assert list(DlxAllocation.objects.values_list("pk", "amount")) == before
    from apps.arena.services import consume_pending_events
    from apps.rewards.models import RewardSettlementOutbox
    from apps.rewards.tasks import consume_outbox
    from apps.seasons.close import advance_close, start_or_resume_close
    from apps.typing_game.models import TypingWallet

    assert TypingWallet.objects.get(player=p).claimable_balance > 0  # No weekly auto-claim.
    monkeypatch.setattr("apps.seasons.close._enqueue", lambda *_a, **_kw: None)
    run = start_or_resume_close(w.season, actor=w.actor, reason="synthetic complete lifecycle")
    for _ in range(20):
        for outbox_id in RewardSettlementOutbox.objects.exclude(state="PROCESSED").values_list(
            "pk", flat=True
        ):
            consume_outbox(outbox_id)
        consume_pending_events()
        state = advance_close(run.pk)
        if state == "COMPLETED":
            break
    run.refresh_from_db()
    assert run.stage == "COMPLETED", run.drain_evidence
    w.season.refresh_from_db()
    assert w.season.status == "CLOSED"
    assert TypingWallet.objects.get(player=p).claimable_balance == 0
    output = {
        "epoch_count": count,
        "epoch_days": days,
        "closing_days": closing,
        "settlement_gate": gate,
        "base": base,
        "maximum": maximum,
        "registered_policy": registered,
        "allocation_total": result["confirmed_allocations"],
        "final_residual": result["final_residual"],
        "correction_reserve_unused": result["unused_correction_reserve"],
        "closed": True,
        "typing_wallet_auto_settled_only_at_close": True,
        "epochs": [
            {
                "number": e.epoch_number,
                "base": e.base_budget,
                "growth": e.growth_budget,
                "categories": [
                    {
                        "category": b.category,
                        "fresh": b.base_epoch_budget + b.growth_contribution,
                        "allocated": b.allocated_amount,
                        "carry_in": b.carry_in,
                        "carry_out": b.carry_out,
                    }
                    for b in e.category_budgets.order_by("category")
                ],
            }
            for e in w.epochs
        ],
    }
    directory = Path("work/phase4-simulations")
    directory.mkdir(parents=True, exist_ok=True)
    prefix = "registered-" if registered else ""
    (directory / f"{prefix}{count}x{days}plus{closing}.json").write_text(
        json.dumps(output, indent=2) + "\n"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "milestones",
    [(), (milestone("a", 1, 10001),), (milestone("a", 1, 10001), milestone("b", 2, 20003))],
)
def test_growth_is_exact_future_funding_and_retry_safe(configurable_world, milestones):
    w = configurable_world(count=3, milestones=milestones, targets=1)
    participants = [w.player() for _ in range(2)]
    for p in participants:
        for day in (0, 1):
            w.typing(p, START + timedelta(days=day, hours=1))
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    assert run_weekly_lifecycle(w.season) == 1
    assert len(unlock_growth(w.season)) == 0
    events = list(GrowthUnlockEvent.objects.all())
    amount = sum(m["unlock_dlx_amount"] for m in milestones)
    assert sum(e.unlocked_amount for e in events) == amount
    assert GrowthEpochFunding.objects.filter(epoch=w.epochs[0]).count() == len(milestones)
    assert sum(GrowthEpochFunding.objects.values_list("amount", flat=True)) == amount
    assert all(e.evidence["policy_checksum"] == w.document.checksum for e in events)
    for epoch in w.epochs[1:]:
        for p in participants:
            for day in (0, 1):
                w.typing(p, epoch.starts_at + timedelta(days=day, hours=1))
        w.clock["at"] = epoch.ends_at
        run_weekly_lifecycle(w.season)
        budget = epoch.category_budgets.get(category="CORE_GAMEPLAY")
        paid = list(
            DlxAllocation.objects.filter(epoch=epoch, category="CORE_GAMEPLAY").values_list(
                "amount", flat=True
            )
        )
        assert paid and all(
            value == (budget.base_epoch_budget + budget.growth_contribution) // 100
            for value in paid
        )
    fresh = sum(
        b.base_epoch_budget + b.growth_contribution
        for e in w.epochs
        for b in e.category_budgets.all()
    )
    assert fresh == 100003 + amount
    for reserve in CategoryCorrectionReserve.objects.select_related("budget"):
        b = reserve.budget
        assert reserve.amount == (b.base_epoch_budget + b.growth_contribution) * 500 // 10000


@pytest.mark.django_db
def test_growth_at_final_cutoff_funds_final_epoch(configurable_world):
    w = configurable_world(count=1, milestones=[milestone("late", 1, 701)])
    p = w.player()
    for day in (0, 1):
        w.typing(p, START + timedelta(days=day, hours=1))
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    assert run_weekly_lifecycle(w.season) == 1
    event = GrowthUnlockEvent.objects.get()
    assert event.unlocked_amount == 701 and event.evidence["outcome"] == "FUNDED"
    assert GrowthEpochFunding.objects.get().epoch_id == w.epochs[0].pk
    assert len(initialize_epochs(w.season)) == 1


@pytest.mark.django_db
def test_authority_pause_old_worker_and_pinned_setup(configurable_world):
    w = configurable_world()
    stranger = User.objects.create_user(username="unprivileged")
    with pytest.raises(PermissionDenied):
        control_automation(
            w.season, enabled=True, actor=stranger, reason="x", evidence_reference="x"
        )
    w.enable()
    with pytest.raises(ValidationError, match="INCOMPATIBLE"):
        run_weekly_lifecycle(w.season, capability="OLD_WORKER")
    control_automation(
        w.season, enabled=False, actor=w.actor, reason="pause", evidence_reference="ticket"
    )
    w.clock["at"] = w.season.ends_at
    assert run_weekly_lifecycle(w.season) == 0
    with pytest.raises(DatabaseError), transaction.atomic():
        Season.objects.filter(pk=w.season.pk).update(ends_at=w.season.ends_at + timedelta(days=1))
    payload = copy.deepcopy(w.config.payload)
    payload["dlx_distribution"]["pool"]["base_dlx_amount"] += 1
    draft = GameConfigVersion.objects.create(
        version="incompatible",
        schema_version=4,
        effective_at=START + timedelta(days=7),
        payload=payload,
    )
    with pytest.raises(ValidationError, match="pinned"):
        publish_game_config(draft, published_at=START)


@pytest.mark.django_db
def test_worker_restart_after_committed_freeze(configurable_world, monkeypatch):
    w = configurable_world()
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    original = __import__("apps.arena.automation", fromlist=["settle_epoch"]).settle_epoch

    def crash(*args, **kwargs):
        raise ConnectionError("synthetic worker connection lost")

    monkeypatch.setattr("apps.arena.automation.settle_epoch", crash)
    with pytest.raises(ConnectionError):
        run_weekly_lifecycle(w.season)
    w.epochs[0].refresh_from_db()
    assert w.epochs[0].state == "FROZEN"
    monkeypatch.setattr("apps.arena.automation.settle_epoch", original)
    assert run_weekly_lifecycle(w.season) == 1
    assert EconomicAutomationAttempt.objects.filter(status="FAILED").count() == 1


@pytest.mark.parametrize(
    "field,value", [("epoch_count", 0), ("product_period_days", 16), ("closing_days", -1)]
)
def test_invalid_timing_fails_before_publication(field, value):
    payload = copy.deepcopy(V2)
    payload["dlx_distribution"]["timing"][field] = value
    with pytest.raises(ValidationError):
        parse_economy_sync(payload)


@pytest.mark.django_db
@pytest.mark.parametrize("count,closing,gate", [(2, 1, 1), (6, 3, 3)])
def test_real_all_five_sources_through_automatic_workers(configurable_world, count, closing, gate):
    from apps.arena.tests.phase3.test_accounting_and_guards import clan_member, contribution
    from apps.arena.tests.phase3.test_lifecycle_isolation import creator_day, registration
    from apps.referrals.models import ReferralRelation
    from apps.referrals.services import activate_relation
    from apps.rewards.services import settle_player_reward

    w = configurable_world(
        count=count, closing=closing, gate=gate, base=60000003, maximum=100000019
    )
    owner = w.player()
    speakers = [w.player() for _ in range(3)]
    member = clan_member(w, owner)
    reg = registration(w, owner)
    w.enable()
    for epoch in w.epochs:
        offset = (epoch.starts_at - START).days
        for p in [owner, *speakers]:
            for day in (0, 1):
                w.typing(p, epoch.starts_at + timedelta(days=day, hours=1))
        for day in (1, 2):
            contribution(
                w,
                member,
                epoch.starts_at + timedelta(days=day, hours=3),
                f"p4-contribution:{epoch.pk}:{day}",
            )
            creator_day(w, reg, speakers, offset + day)
        if epoch.epoch_number == 1:
            invitee = speakers[0]
            at = epoch.starts_at + timedelta(days=3)
            w.clock["at"] = at
            settle_player_reward(
                player=invitee,
                source_type="ADMIN_ADJUSTMENT",
                source_id="synthetic-level",
                context="TEST",
                amount=3000,
                idempotency_key="synthetic-level",
                occurred_at=at,
                resolved_config=w.resolved,
            )
            relation = ReferralRelation.objects.create(
                inviter=owner, invitee=invitee, attributed_at=START
            )
            assert activate_relation(relation.pk, at=at)
        w.clock["at"] = epoch.ends_at
        run_weekly_lifecycle(w.season)
    w.clock["at"] = w.season.ends_at
    run_weekly_lifecycle(w.season)
    first = w.epochs[0]
    assert set(
        first.performance_snapshots.filter(player=owner, performance_units__gt=0).values_list(
            "category", flat=True
        )
    ) == set(CATEGORIES)
    assert set(
        DlxAllocation.objects.filter(epoch=first, player=owner).values_list("category", flat=True)
    ) == set(CATEGORIES)
    assert all(all(reconcile_epoch(e).values()) for e in w.epochs)


@pytest.mark.django_db(transaction=True)
def test_concurrent_weekly_workers_and_growth_are_exactly_once(configurable_world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connections

    w = configurable_world(count=3, milestones=[milestone("once", 1, 10001)])
    p = w.player()
    for day in (0, 1):
        w.typing(p, START + timedelta(days=day, hours=1))
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return run_weekly_lifecycle(w.season)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sum(results) == 1
    assert GrowthUnlockEvent.objects.count() == 1
    assert sum(GrowthEpochFunding.objects.values_list("amount", flat=True)) == 10001


@pytest.mark.django_db(transaction=True)
def test_terminated_process_releases_worker_lock_and_resumes(configurable_world):
    import os
    import subprocess
    import sys

    from django.db import connection

    w = configurable_world()
    w.enable()
    at = w.epochs[0].ends_at
    env = dict(os.environ)
    env["POSTGRES_DB"] = connection.settings_dict["NAME"]
    source = f"""
import os,django
from datetime import datetime
django.setup()
from django.utils import timezone
from apps.seasons.models import Season
from apps.arena import automation
timezone.now=lambda:datetime.fromisoformat({at.isoformat()!r})
automation.settle_epoch=lambda *args,**kwargs:os._exit(23)
automation.run_weekly_lifecycle(Season.objects.get(pk={w.season.pk}))
"""
    result = subprocess.run(
        [sys.executable, "-c", source], env=env, capture_output=True, timeout=60
    )
    assert result.returncode == 23, result.stderr.decode()
    w.epochs[0].refresh_from_db()
    assert w.epochs[0].state == "FROZEN"
    assert EconomicAutomationAttempt.objects.filter(status="RUNNING").exists()
    w.clock["at"] = at
    assert run_weekly_lifecycle(w.season) == 1
    assert EconomicAutomationAttempt.objects.filter(status="INTERRUPTED").exists()


@pytest.mark.django_db
def test_signed_bigint_accounting_bound_and_no_lost_remainders(configurable_world):
    base = 2**63 - 101
    w = configurable_world(base=base, maximum=2**63 - 1)
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    assert run_weekly_lifecycle(w.season) == 1
    budgets = list(w.epochs[0].category_budgets.all())
    assert sum(b.base_epoch_budget for b in budgets) == w.epochs[0].base_budget
    assert all(reconcile_epoch(w.epochs[0]).values())
    bad = copy.deepcopy(w.config.payload)
    bad["dlx_distribution"]["pool"]["max_dlx_amount"] = 2**63
    with pytest.raises(ValidationError, match="accounting bounds"):
        parse_economy_sync(bad)


@pytest.mark.django_db
def test_midseason_daily_typing_cutovers_preserve_existing_entitlements(configurable_world):
    from apps.daily.services import claim_daily
    from apps.typing_game.models import TypingWalletTransaction
    from apps.typing_game.services import claim_wallet

    w = configurable_world()
    p = w.player()
    at = w.epochs[1].starts_at
    changed = copy.deepcopy(w.config.payload)
    changed["daily"]["reward_cycle_points"] = [
        x * 2 for x in changed["daily"]["reward_cycle_points"]
    ]
    for bracket in changed["typing"]["accuracy_reward_brackets"]:
        bracket["points"] += 1
    changed["typing"]["max_reward_per_attempt"] += 1
    # Maximum remains a bound for all configured reward brackets.
    draft = GameConfigVersion.objects.create(
        version="future-only", schema_version=4, effective_at=at, payload=changed
    )
    publish_game_config(draft, published_at=START)
    w.clock["at"] = at - timedelta(seconds=2)
    attempt, _ = start_attempt(p, "in-flight-before-cutover", w.clock["at"])
    first, _ = claim_daily(p, w.clock["at"])
    w.clock["at"] = at + timedelta(seconds=3)
    completed, _ = submit_attempt(p, attempt.public_id, TEXT, w.clock["at"])
    assert completed.config_version_id == w.config.pk
    second, created = claim_daily(p, w.clock["at"])
    assert not created and first.pk == second.pk
    assert second.config_version_id == w.config.pk
    credit = TypingWalletTransaction.objects.get(kind="CREDIT", attempt=completed)
    claim_wallet(p, "manual-after-cutover", w.clock["at"])
    assert credit.config_version_id == w.config.pk
    assert credit.amount == completed.reward_amount
    w.clock["at"] = at + timedelta(days=1)
    next_claim, _ = claim_daily(p, w.clock["at"])
    assert next_claim.config_version_id == draft.pk


@pytest.mark.django_db
def test_existing_draft_epoch_commitments_cannot_be_silently_repriced(phase4_config_factory):
    config = phase4_config_factory(effective_at=START - timedelta(days=3))
    season = Season.objects.create(
        code="draft-commitments",
        title="draft",
        baseline_config=config,
        starts_at=START,
        ends_at=START + timedelta(days=45),
    )
    original = initialize_epochs(season)

    def change(payload):
        payload["dlx_distribution"]["pool"]["base_dlx_amount"] += 1

    replacement = phase4_config_factory(effective_at=START - timedelta(days=2), mutate=change)
    season.baseline_config = replacement
    season.save(update_fields=["baseline_config"])
    with pytest.raises(ValidationError, match="commitment conflicts"):
        initialize_epochs(season)
    original[0].refresh_from_db()
    assert original[0].base_budget == 10000000


@pytest.mark.django_db
def test_one_day_close_funds_final_epoch_late_source_correction(configurable_world):
    from apps.arena.corrections import (
        assess_correction,
        corrected_category_scores,
        pay_correction_round,
    )
    from apps.arena.models import EconomicDeferral

    w = configurable_world(base=3000000, maximum=5000000)
    original, omitted = w.player(), w.player()
    w.enable()
    for epoch in w.epochs:
        for day in (0, 1):
            w.typing(original, epoch.starts_at + timedelta(days=day, hours=1))
        w.clock["at"] = epoch.ends_at
        run_weekly_lifecycle(w.season)
    before = list(DlxAllocation.objects.values_list("pk", "amount"))
    final = w.epochs[-1]
    # Replay late-discovered authoritative source facts with their original earned timestamps.
    for day in (0, 1):
        w.typing(omitted, final.starts_at + timedelta(days=day, hours=1))
    score, raw = corrected_category_scores(final)[omitted.pk]["CORE_GAMEPLAY"]
    for case in EconomicDeferral.objects.filter(
        epoch=final, player=omitted, category="CORE_GAMEPLAY"
    ):
        assess_correction(
            case, performance_units=score, actor=w.actor, evidence_reference="late source verified"
        )
    w.clock["at"] = w.season.ends_at
    paid = []
    for epoch in w.epochs:
        for category in CATEGORIES:
            correction = finalize_correction_round(epoch, category, actor=w.actor)
            paid.extend(
                pay_correction_round(
                    correction, actor=w.actor, idempotency_key=f"short-close:{epoch.pk}:{category}"
                )
            )
    assert any(result.outcome == "PAID" for result in paid)
    assert list(DlxAllocation.objects.values_list("pk", "amount")) == before
    result = reconcile_season_dlx(w.season, record_residuals=True)
    assert result["reconciled"] and result["funded_corrections"] > 0
    assert (
        result["confirmed_allocations"] + result["funded_corrections"] + result["final_residual"]
        == 3000000
    )
