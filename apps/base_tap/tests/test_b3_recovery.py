"""Real PostgreSQL paired-device ownership and receipt regressions."""

# mypy: disable-error-code="no-untyped-def"
from datetime import timedelta
from uuid import uuid4

import pytest

from apps.base_tap.tests.test_v2 import AT, batch, world  # noqa: F401, F811
from apps.base_tap.v2 import TapError, issue_lease, release_lease, state_snapshot, submit_batch

pytestmark = pytest.mark.django_db(transaction=True)


def test_device_switch_fences_old_and_requires_intent_even_after_expiry(world):  # noqa: F811
    player, phone, state, _, _ = world
    desktop = uuid4()
    with pytest.raises(TapError, match="ACTIVE_SESSION"):
        issue_lease(player, session=desktop, at=AT)
    assert state_snapshot(player, session=desktop, at=AT)["ownership"]["status"] == "ANOTHER_DEVICE"
    switched = issue_lease(
        player, session=desktop, at=AT, takeover=True, expected_generation=state["generation"]
    )
    assert switched["burst_remaining"] == 50
    with pytest.raises(TapError, match="LEASE_REVOKED"):
        submit_batch(player, batch(state), at=AT)
    with pytest.raises(TapError, match="LEASE_REVOKED"):
        issue_lease(player, session=phone, at=AT + timedelta(seconds=121))
    recovered = issue_lease(
        player,
        session=phone,
        at=AT + timedelta(seconds=121),
        takeover=True,
        expected_generation=switched["generation"],
    )
    assert recovered["ownership"]["status"] == "OWNED"


def test_stale_release_cannot_revoke_successor_and_receipt_survives(world):  # noqa: F811
    player, phone, state, _, _ = world
    receipt = submit_batch(player, batch(state, count=7), at=AT)
    desktop = uuid4()
    switched = issue_lease(
        player, session=desktop, at=AT, takeover=True, expected_generation=state["generation"]
    )
    old = state["lease"]
    release_lease(
        player, lease_id=old["lease_id"], session=phone, generation=old["generation"], at=AT
    )
    assert (
        state_snapshot(player, session=desktop, at=AT)["lease"]["lease_id"]
        == switched["lease"]["lease_id"]
    )
    replay = submit_batch(player, batch(state, count=7), at=AT)
    assert replay["duplicate"] and replay["receipt"] == receipt["receipt"]
    assert replay["snapshot"]["progression"]["lifetime_points"] == 7
    own = switched["lease"]
    release_lease(
        player, lease_id=own["lease_id"], session=desktop, generation=own["generation"], at=AT
    )
    regained = issue_lease(
        player, session=phone, at=AT, takeover=True, expected_generation=switched["generation"]
    )
    assert regained["burst_remaining"] == 43


def test_export_session_fixture(world):  # noqa: F811
    import json
    import os
    from pathlib import Path

    from apps.base_tap.schemas import TapStateOutput

    player, phone, state, _, _ = world
    desktop = uuid4()
    blocked = state_snapshot(player, session=desktop, at=AT)
    switched = issue_lease(
        player, session=desktop, at=AT, takeover=True, expected_generation=state["generation"]
    )
    moved = state_snapshot(player, session=phone, at=AT)
    fixture = {
        "provenance": "Synthetic PostgreSQL test output; not production balances",
        "initial": TapStateOutput.model_validate(state).model_dump(mode="json"),
        "another_device": TapStateOutput.model_validate(blocked).model_dump(mode="json"),
        "switch_request": {
            "session_id": str(desktop),
            "takeover": True,
            "expected_generation": state["generation"],
        },
        "switch_response": TapStateOutput.model_validate(switched).model_dump(mode="json"),
        "moved": TapStateOutput.model_validate(moved).model_dump(mode="json"),
    }
    if os.getenv("B3_EXPORT_FIXTURES"):
        Path("docs/fixtures/b3/session-switch.json").write_text(
            json.dumps(fixture, indent=2) + "\n"
        )


def test_parallel_takeover_one_winner_and_old_client_cannot_ping_pong(world):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections

    player, phone, state, _, _ = world
    generation = state["generation"]

    def take(_):
        close_old_connections()
        try:
            try:
                return issue_lease(
                    player, session=uuid4(), at=AT, takeover=True, expected_generation=generation
                )
            except TapError as exc:
                return exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(take, range(2)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert "GENERATION_STALE" in results
    with pytest.raises(TapError, match="LEASE_REVOKED"):
        issue_lease(player, session=phone, at=AT)


def test_cooldown_switch_and_daily_mission_rewards_keep_history(world):  # noqa: F811
    from apps.daily.services import claim_daily
    from apps.game_config.services import resolve_effective_game_config
    from apps.missions.models import MissionDefinition
    from apps.missions.services import claim_mission, start_mission, verify_mission
    from apps.rewards.services import settle_player_reward

    player, phone, state, config, season = world
    resolved = resolve_effective_game_config(AT)
    settle_player_reward(
        player=player,
        source_type="ADMIN_ADJUSTMENT",
        source_id="historical",
        context="HISTORY",
        amount=150,
        idempotency_key="history150",
        occurred_at=AT,
        resolved_config=resolved,
    )
    receipt = submit_batch(player, batch(state, count=50), at=AT)
    desktop = uuid4()
    with pytest.raises(TapError, match="COOLDOWN"):
        issue_lease(
            player,
            session=desktop,
            at=AT + timedelta(seconds=1),
            takeover=True,
            expected_generation=state["generation"],
        )
    claim_daily(player, AT + timedelta(seconds=2))
    MissionDefinition.objects.create(
        key="visit-dalexio",
        version=1,
        title="Visit",
        enabled=True,
        verification_family="TIMED_EXTERNAL",
        repeat_policy="ONCE_PER_DEFINITION_VERSION",
        verification_parameters={"destination_url": "https://dalexio.com"},
    )
    opened = start_mission(player, "visit-dalexio", AT)
    verify_mission(player, "visit-dalexio", AT + timedelta(seconds=7), opened.open_token)
    claim_mission(player, "visit-dalexio", AT + timedelta(seconds=8))
    switched = issue_lease(
        player,
        session=desktop,
        at=AT + timedelta(seconds=10),
        takeover=True,
        expected_generation=state["generation"],
    )
    assert switched["progression"]["lifetime_points"] == 150 + 50 + 80 + 120
    assert switched["season_points"] == 400 and switched["burst_remaining"] == 50
    assert receipt["earned_points"] == 50


def test_offline_expired_unconfirmed_batch_never_becomes_reward_and_new_client_recovers(world):  # noqa: F811
    player, phone, state, _, _ = world
    accepted = submit_batch(player, batch(state, count=7), at=AT)
    later = AT + timedelta(seconds=121)
    with pytest.raises(TapError, match="LEASE_EXPIRED"):
        submit_batch(player, batch(state, start=8, key="unconfirmed"), at=later)
    replay = submit_batch(player, batch(state, count=7), at=later)
    assert replay["receipt"] == accepted["receipt"]
    new = issue_lease(player, session=uuid4(), at=later)
    assert new["burst_remaining"] == 43 and new["season_points"] == 7


def test_recovery_latency_report(world, player_factory):  # noqa: F811
    import json
    import os
    from pathlib import Path
    from statistics import median
    from time import perf_counter

    _, _, _, _, _ = world
    results: dict[str, list[float]] = {
        name: []
        for name in [
            "explicit_switch",
            "release_and_regain",
            "both_closed_new_client",
            "both_closed_explicit_recovery",
            "lost_response_replay",
            "expired_lease_pending_batch",
        ]
    }
    for index in range(20):
        player = player_factory(5000 + index)
        phone = uuid4()
        initial = issue_lease(player, session=phone, at=AT)
        payload = batch(initial, count=1, key=f"measured-{index}")
        submit_batch(player, payload, at=AT)
        desktop = uuid4()
        start = perf_counter()
        observed = state_snapshot(player, session=desktop, at=AT)
        switched = issue_lease(
            player,
            session=desktop,
            at=AT,
            takeover=True,
            expected_generation=observed["generation"],
        )
        results["explicit_switch"].append((perf_counter() - start) * 1000)
        lease = switched["lease"]
        start = perf_counter()
        release_lease(
            player,
            lease_id=lease["lease_id"],
            session=desktop,
            generation=lease["generation"],
            at=AT,
        )
        regained = issue_lease(
            player, session=phone, at=AT, takeover=True, expected_generation=switched["generation"]
        )
        results["release_and_regain"].append((perf_counter() - start) * 1000)
        start = perf_counter()
        new_client = uuid4()
        observed = state_snapshot(player, session=new_client, at=AT)
        issue_lease(
            player,
            session=new_client,
            at=AT,
            takeover=True,
            expected_generation=observed["generation"],
        )
        results["both_closed_explicit_recovery"].append((perf_counter() - start) * 1000)
        later = AT + timedelta(seconds=121)
        start = perf_counter()
        issue_lease(player, session=uuid4(), at=later)
        results["both_closed_new_client"].append((perf_counter() - start) * 1000)
        start = perf_counter()
        replay = submit_batch(player, payload, at=later)
        assert replay["duplicate"]
        results["lost_response_replay"].append((perf_counter() - start) * 1000)
        start = perf_counter()
        with pytest.raises(TapError):
            submit_batch(player, batch(regained, start=2, key="expired-pending"), at=later)
        results["expired_lease_pending_batch"].append((perf_counter() - start) * 1000)
    report = {
        "environment": (
            "Local synthetic PostgreSQL + Redis, sequential service transactions; "
            "no WAN/WebView latency"
        ),
        "samples_per_scenario": 20,
        "passive_expiry_seconds": 120,
        "cooldown_wait_seconds": 10,
        "scenarios": {
            name: {
                "p50_ms": round(median(values), 3),
                "p95_ms": round(sorted(values)[18], 3),
                "max_ms": round(max(values), 3),
            }
            for name, values in results.items()
        },
    }
    assert all(max(values) < 20000 for values in results.values())
    if os.getenv("B3_EXPORT_FIXTURES"):
        Path("docs/fixtures/b3/session-recovery-latency.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )


def test_daily_and_mission_settlement_parallel_with_takeover_stay_canonical(world):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections

    from apps.daily.services import claim_daily
    from apps.missions.models import MissionDefinition
    from apps.missions.services import claim_mission, start_mission, verify_mission

    player, phone, state, _, _ = world
    submit_batch(player, batch(state, count=7), at=AT)
    MissionDefinition.objects.create(
        key="visit-dalexio",
        version=1,
        title="Visit",
        enabled=True,
        verification_family="TIMED_EXTERNAL",
        repeat_policy="ONCE_PER_DEFINITION_VERSION",
        verification_parameters={"destination_url": "https://dalexio.com"},
    )
    opened = start_mission(player, "visit-dalexio", AT)
    verify_mission(player, "visit-dalexio", AT + timedelta(seconds=7), opened.open_token)
    desktop = uuid4()

    def run(kind):
        close_old_connections()
        try:
            if kind == "switch":
                return issue_lease(
                    player,
                    session=desktop,
                    at=AT + timedelta(seconds=8),
                    takeover=True,
                    expected_generation=state["generation"],
                )
            if kind == "daily":
                return claim_daily(player, AT + timedelta(seconds=8))
            return claim_mission(player, "visit-dalexio", AT + timedelta(seconds=8))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(run, ["switch", "daily", "mission"]))
    final = state_snapshot(player, session=desktop, at=AT + timedelta(seconds=9))
    assert final["progression"]["lifetime_points"] == 207 and final["season_points"] == 207
    assert final["burst_remaining"] == 43 and final["ownership"]["status"] == "OWNED"


def test_lease_cleanup_cannot_erase_fenced_session_history(world):  # noqa: F811
    from apps.base_tap.models import TapGameplaySession
    from apps.base_tap.v2 import purge_expired_leases

    player, phone, state, _, _ = world
    desktop = uuid4()
    switched = issue_lease(
        player, session=desktop, at=AT, takeover=True, expected_generation=state["generation"]
    )
    # Exercise retention of a B2-era lease whose durable marker was not yet created.
    TapGameplaySession.objects.filter(player=player, session_id=phone).delete()
    later = AT + timedelta(days=8)
    assert purge_expired_leases(at=later) == 2
    with pytest.raises(TapError, match="LEASE_REVOKED"):
        issue_lease(player, session=phone, at=later)
    restored = issue_lease(
        player, session=phone, at=later, takeover=True, expected_generation=switched["generation"]
    )
    assert restored["ownership"]["status"] == "OWNED"
