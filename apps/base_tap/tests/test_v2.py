import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import connection, connections
from django.test import Client

from apps.arena.models import PerformanceSourceFact
from apps.base_tap.models import BaseTapDailyAccumulator, TapBurstState
from apps.base_tap.schemas import BatchInput, BatchOutput, TapStateOutput
from apps.base_tap.services import finalize_tap_accumulator, submit_tap_batch
from apps.base_tap.v2 import (
    TapError,
    issue_lease,
    purge_expired_leases,
    submit_batch,
)
from apps.game_config.services import parse_economy_sync, resolve_effective_game_config
from apps.progression.models import PlayerProgression
from apps.rewards.models import RewardLedger, RewardSettlementOutbox
from apps.rewards.services import settle_player_reward
from apps.seasons.models import PlayerSeason

AT = datetime(2026, 1, 2, tzinfo=UTC)
SAMPLE = json.loads(
    (
        Path(__file__).resolve().parents[3] / "config/examples/season1.game-config.v2.json"
    ).read_text()
)


@pytest.fixture
def world(
    settings: Any, phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> Any:
    settings.TAP_V2_ISSUANCE_ENABLED = True
    settings.BASE_TAP_LEGACY_INGRESS_ENABLED = False
    settings.TAP_V2_PLAYER_REQUESTS = 10000
    settings.TAP_V2_SESSION_REQUESTS = 10000

    def mutate(payload: dict[str, Any]) -> None:
        payload.update(copy.deepcopy(SAMPLE))

    config = phase4_config_factory(effective_at=AT - timedelta(days=1), mutate=mutate)
    season = season_factory(config, starts_at=AT - timedelta(days=1))
    player = player_factory()
    session = uuid4()
    state = issue_lease(player, session=session, at=AT)
    return player, session, state, config, season


def batch(state: dict[str, Any], *, count: int = 1, start: int = 1, key: str = "one") -> BatchInput:
    lease = state["lease"]
    return BatchInput(
        lease_id=lease["lease_id"],
        session_id=lease["session_id"],
        generation=lease["generation"],
        sequence_start=start,
        tap_count=count,
        idempotency_key=key,
    )


def error(code: str) -> Any:
    return pytest.raises(TapError, match=code)


@pytest.mark.django_db
def test_first_and_partial_50_51_instant_and_no_referral(world: Any) -> None:
    player, session, state, _, season = world
    TapStateOutput.model_validate(state)
    first = submit_batch(player, batch(state), at=AT)
    BatchOutput.model_validate(first)
    assert first["snapshot"]["progression"]["lifetime_points"] == 1
    assert PlayerSeason.objects.get(player=player, season=season).season_points == 1
    result = submit_batch(
        player, batch(state, count=50, start=2, key="tail"), at=AT + timedelta(seconds=1)
    )
    assert result["accepted_taps"] == 49 and result["rejected_taps"] == 1
    assert result["snapshot"]["burst_remaining"] == 0
    with error("ALLOWANCE_EXHAUSTED"):
        submit_batch(player, batch(state, start=51, key="51"), at=AT + timedelta(seconds=2))
    assert RewardLedger.objects.count() == 2
    assert PerformanceSourceFact.objects.count() == 2
    assert not RewardSettlementOutbox.objects.exists()
    assert sum(RewardLedger.objects.values_list("player_amount", flat=True)) == 50
    assert all(
        x.source_type == "BASE_TAP" and x.referral_amount == 0 and x.clan_treasury_amount == 0
        for x in RewardLedger.objects.all()
    )


@pytest.mark.django_db
def test_cooldown_99_vs_10_unlimited_and_utc_rollover(world: Any) -> None:
    player, session, state, _, _ = world
    now = AT
    for index in range(5):
        submit_batch(player, batch(state, count=50, key=f"burst{index}"), at=now)
        with error("COOLDOWN"):
            issue_lease(player, session=session, at=now + timedelta(seconds=9.9))
        now += timedelta(seconds=10)
        state = issue_lease(player, session=session, at=now)
    now = AT + timedelta(days=1)
    state = issue_lease(player, session=session, at=now)
    submit_batch(player, batch(state, count=50, key="tomorrow"), at=now)
    assert PlayerProgression.objects.get(player=player).lifetime_points == 300


@pytest.mark.django_db
def test_lost_response_duplicate_after_expiry_cleanup_and_ban(world: Any) -> None:
    player, _, state, _, _ = world
    payload = batch(state, count=7)
    first = submit_batch(player, payload, at=AT)
    purge_expired_leases(at=AT + timedelta(days=8))
    player.moderation_state = "BANNED"
    player.save()
    retry = submit_batch(player, payload, at=AT + timedelta(days=8))
    assert retry["duplicate"] and retry["receipt_id"] == first["receipt_id"]
    assert retry["accepted_taps"] == 7
    assert RewardLedger.objects.count() == 1
    with error("IDEMPOTENCY_CONFLICT"):
        submit_batch(player, payload.model_copy(update={"tap_count": 8}), at=AT)


@pytest.mark.django_db
def test_gap_replay_expiry_takeover_fencing_and_interrupted_burst(world: Any) -> None:
    player, session, state, _, _ = world
    with error("SEQUENCE_GAP"):
        submit_batch(player, batch(state, start=2), at=AT)
    submit_batch(player, batch(state, count=20), at=AT)
    with error("SEQUENCE_REPLAY"):
        submit_batch(player, batch(state, key="different"), at=AT)
    other = uuid4()
    with error("ACTIVE_SESSION"):
        issue_lease(player, session=other, at=AT)
    with error("GENERATION_STALE"):
        issue_lease(player, session=other, at=AT, takeover=True, expected_generation=0)
    taken = issue_lease(player, session=other, at=AT, takeover=True, expected_generation=1)
    assert taken["burst_remaining"] == 30
    with error("LEASE_REVOKED"):
        submit_batch(player, batch(state, start=21, key="old"), at=AT)
    with error("LEASE_EXPIRED"):
        submit_batch(player, batch(taken, start=21, key="late"), at=AT + timedelta(seconds=120))
    with error("LEASE_REVOKED"):
        issue_lease(player, session=session, at=AT + timedelta(seconds=120))
    renewed = issue_lease(
        player,
        session=session,
        at=AT + timedelta(seconds=120),
        takeover=True,
        expected_generation=taken["generation"],
    )
    assert renewed["burst_remaining"] == 30 and renewed["lease"]["sequence_start"] == 21


@pytest.mark.django_db
@pytest.mark.parametrize("moderation", ["BANNED", "SUSPENDED"])
def test_moderation_and_rollback(world: Any, monkeypatch: Any, moderation: str) -> None:
    player, session, state, _, _ = world
    player.moderation_state = moderation
    player.save()
    with error("PLAYER_RESTRICTED"):
        submit_batch(player, batch(state), at=AT)
    with error("PLAYER_RESTRICTED"):
        issue_lease(player, session=session, at=AT)
    player.moderation_state = "ACTIVE"
    player.save()

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("cancelled transaction")

    monkeypatch.setattr(PerformanceSourceFact.objects, "create", fail)
    with pytest.raises(RuntimeError):
        submit_batch(player, batch(state), at=AT)
    assert not RewardLedger.objects.exists()
    assert not PlayerProgression.objects.exists()
    assert not PlayerSeason.objects.exists()
    assert TapBurstState.objects.get(player=player).remaining == 50


@pytest.mark.django_db(transaction=True)
def test_two_devices_and_concurrent_microbatches_postgres(world: Any) -> None:
    assert connection.vendor == "postgresql"
    player, _, state, _, _ = world
    # expire initial authorization; both devices race for replacement of same burst
    gate = Barrier(2)

    def issue(_: int) -> Any:
        connections.close_all()
        try:
            gate.wait(timeout=10)
            try:
                return issue_lease(player, session=uuid4(), at=AT + timedelta(seconds=120))
            except TapError as exc:
                return exc.code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(issue, range(2)))
    assert results.count("ACTIVE_SESSION") == 1
    chosen = next(x for x in results if isinstance(x, dict))
    payload = batch(chosen, count=40)
    gate = Barrier(2)

    def submit(_: int) -> Any:
        connections.close_all()
        try:
            gate.wait(timeout=10)
            return submit_batch(player, payload, at=AT + timedelta(seconds=120))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sum(x["duplicate"] for x in results) == 1
    assert RewardLedger.objects.count() == 1
    assert TapBurstState.objects.get().remaining == 10


@pytest.mark.django_db
def test_historical_150_points_level_and_rollback_flags(world: Any, settings: Any) -> None:
    player, session, state, _, _ = world
    settle_player_reward(
        player=player,
        source_type="TEST",
        source_id="historical150",
        context="TEST",
        amount=150,
        idempotency_key="historical150",
        occurred_at=AT,
        resolved_config=resolve_effective_game_config(AT),
        create_settlement_outbox=False,
    )
    settings.TAP_V2_ISSUANCE_ENABLED = False
    result = submit_batch(player, batch(state, count=50), at=AT)
    assert result["snapshot"]["progression"]["lifetime_points"] == 200
    assert result["snapshot"]["progression"]["next_threshold"] == 300
    assert RewardLedger.objects.get(idempotency_key="historical150").player_amount == 150
    with error("ISSUANCE_DISABLED"):
        issue_lease(player, session=session, at=AT + timedelta(seconds=10))


@pytest.mark.django_db
def test_config_pinned_burst_then_new_policy(world: Any, phase4_config_factory: Any) -> None:
    player, session, state, config, _ = world

    def mutate(payload: dict[str, Any]) -> None:
        payload.update(copy.deepcopy(SAMPLE))
        payload["base_tap"]["points_per_rewarded_tap"] = 2
        payload["base_tap"]["rewarded_taps_per_burst"] = 60
        payload["base_tap"]["cooldown_seconds"] = 23

    new = phase4_config_factory(
        effective_at=AT + timedelta(seconds=1), mutate=mutate, version="v2-cutover"
    )
    assert state["lease"]["cooldown_seconds"] == 10
    pinned = issue_lease(player, session=session, at=AT + timedelta(seconds=2))
    assert pinned["lease"]["cooldown_seconds"] == 10
    result = submit_batch(player, batch(state, count=50), at=AT + timedelta(seconds=2))
    assert result["earned_points"] == 50
    assert RewardLedger.objects.get().config_version == config
    next_state = issue_lease(player, session=session, at=AT + timedelta(seconds=12))
    assert result["snapshot"]["cooldown_until"] == AT + timedelta(seconds=12)
    assert next_state["lease"]["cooldown_seconds"] == 23
    assert next_state["lease"]["reward_per_tap"] == 2
    assert next_state["burst_remaining"] == 60
    assert next_state["config_version"] == new.version


@pytest.mark.django_db(transaction=True)
def test_legacy_drain_concurrent_exactly_once(
    settings: Any, phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    config = phase4_config_factory(effective_at=AT - timedelta(days=1))
    season_factory(config, starts_at=AT - timedelta(days=1))
    player = player_factory()
    submit_tap_batch(player, tap_count=25, idempotency_key="legacy", at=AT)
    accumulator = BaseTapDailyAccumulator.objects.get()
    settings.BASE_TAP_LEGACY_INGRESS_ENABLED = False
    report = StringIO()
    call_command("reconcile_legacy_taps", stdout=report)
    assert json.loads(report.getvalue())["rows"][0]["consistent"]
    assert not RewardLedger.objects.exists()

    def drain(_: int) -> None:
        connections.close_all()
        try:
            finalize_tap_accumulator(accumulator.pk, at=AT + timedelta(seconds=1), force=True)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(drain, range(4)))
    accumulator.refresh_from_db()
    assert accumulator.settled_points == 25 and accumulator.pending_points == 0
    assert RewardLedger.objects.count() == 1
    assert PlayerProgression.objects.get().lifetime_points == 25


def test_v2_config_and_openapi() -> None:
    tap, _ = parse_economy_sync(SAMPLE)
    assert tap.protocol_version == 2 and tap.rewarded_taps_per_day == 0
    assert tap.rewarded_taps_per_burst == 50 and tap.cooldown_seconds == 10
    from dexo_land.api import api

    paths = api.get_openapi_schema()["paths"]
    assert all(f"/api/v1/base/v2/{x}" in paths for x in ("tap-state", "tap-leases", "tap-batches"))
    assert "/api/v1/base/taps" in paths


@pytest.mark.django_db
def test_epoch_and_season_edges(world: Any) -> None:
    player, session, state, _, season = world
    edge = season.starts_at + timedelta(days=7)
    state = issue_lease(player, session=session, at=edge - timedelta(seconds=1))
    assert state["lease"]["expires_at"] == edge
    submit_batch(player, batch(state, key="before-epoch"), at=edge - timedelta(milliseconds=50))
    state = issue_lease(player, session=session, at=edge)
    submit_batch(player, batch(state, start=2, key="after-epoch"), at=edge)
    facts = list(PerformanceSourceFact.objects.order_by("source_time"))
    assert facts[0].source_time < edge <= facts[1].source_time
    with error("EPOCH_FINALIZED"):
        submit_batch(
            player, batch(state, start=3, key="closing"), at=season.starts_at + timedelta(days=42)
        )
    assert PlayerProgression.objects.get().lifetime_points == 2


@pytest.mark.django_db
def test_rate_limit_and_other_player_authorization(world: Any, player_factory: Any) -> None:
    player, _, state, _, _ = world
    submit_batch(player, batch(state), at=AT)
    with error("RATE_LIMITED"):
        submit_batch(player, batch(state, start=2, key="fast"), at=AT + timedelta(milliseconds=49))
    assert RewardLedger.objects.count() == 1
    result = submit_batch(
        player, batch(state, start=2, key="fast"), at=AT + timedelta(milliseconds=50)
    )
    assert result["accepted_taps"] == 1
    with error("LEASE_UNKNOWN"):
        submit_batch(player_factory(1234), batch(state, key="stolen"), at=AT)


@pytest.mark.django_db
def test_typed_authenticated_api_and_input_errors(
    world: Any, settings: Any, telegram_bot_token: str, init_data_factory: Any, monkeypatch: Any
) -> None:
    player, session, _, _, _ = world
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    monkeypatch.setattr("apps.base_tap.api.timezone.now", lambda: AT)
    from apps.telegram.auth import verify_init_data

    # Only deterministic validation time changes; HMAC verification remains real.
    monkeypatch.setattr(
        "apps.telegram.mini_app_auth.verify_init_data",
        lambda raw, **kwargs: verify_init_data(raw, now=AT, **kwargs),
    )
    raw = init_data_factory(
        user={"id": player.telegram_user_id, "first_name": "Dexo"}, auth_date=AT
    )
    headers = {"Authorization": f"tma {raw}"}
    client = Client()
    assert client.get("/api/v1/base/v2/tap-state").status_code == 401
    response = client.post(
        "/api/v1/base/v2/tap-leases",
        data=json.dumps({"session_id": str(session)}),
        content_type="application/json",
        headers=headers,
    )
    assert response.status_code == 200
    state = response.json()
    payload = batch(state, count=3)
    response = client.post(
        "/api/v1/base/v2/tap-batches",
        data=payload.model_dump_json(),
        content_type="application/json",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["snapshot"]["progression"]["lifetime_points"] == 3
    assert response["Cache-Control"] == "private, no-store"
    bad = payload.model_dump(mode="json")
    bad["xp"] = 100000
    response = client.post(
        "/api/v1/base/v2/tap-batches",
        data=json.dumps(bad),
        content_type="application/json",
        headers=headers,
    )
    assert response.status_code == 422 and response.json()["code"] == "REQUEST_INVALID"


@pytest.mark.django_db
def test_legacy_processing_crash_reconciles_without_second_credit(
    settings: Any, phase4_config_factory: Any, season_factory: Any, player_factory: Any
) -> None:
    config = phase4_config_factory(effective_at=AT - timedelta(days=1))
    season_factory(config, starts_at=AT - timedelta(days=1))
    player = player_factory()
    submit_tap_batch(player, tap_count=25, idempotency_key="legacy-crash", at=AT)
    row = BaseTapDailyAccumulator.objects.get()
    # Simulate old finalizer's committed-ledger/uncommitted-accumulator crash window.
    settle_player_reward(
        player=player,
        source_type="BASE_TAP",
        source_id=str(row.pk),
        context="CORE_GAMEPLAY",
        amount=25,
        idempotency_key=f"base-tap-day:{row.pk}",
        occurred_at=AT,
        resolved_config=resolve_effective_game_config(AT),
        create_settlement_outbox=False,
    )
    row.settlement_state = "PROCESSING"
    row.lease_until = AT + timedelta(days=1)
    row.save()
    settings.BASE_TAP_LEGACY_INGRESS_ENABLED = False
    output = StringIO()
    before_output = StringIO()
    call_command("reconcile_legacy_taps", stdout=before_output)
    call_command("reconcile_legacy_taps", apply=True, stdout=output)
    report = json.loads(output.getvalue())
    import os

    if os.getenv("B2_EVIDENCE") == "1":
        Path("work").mkdir(exist_ok=True)
        Path("work/legacy-drain-evidence.json").write_text(
            json.dumps(
                {
                    "scope": "isolated synthetic legacy crash-window reconciliation",
                    "dry_run": json.loads(before_output.getvalue()),
                    "apply": report,
                },
                indent=2,
            )
            + "\n"
        )
    assert report["rows"][0]["after_pending"] == 0
    assert PlayerProgression.objects.get().lifetime_points == 25
    assert RewardLedger.objects.count() == 1
    retry = submit_tap_batch(
        player, tap_count=25, idempotency_key="legacy-crash", at=AT + timedelta(days=1)
    )
    assert retry.accepted_taps == 25 and not retry.created and retry.pending_points == 0


@pytest.mark.django_db(transaction=True)
def test_epoch_freeze_exclusive_lock_waits_for_microbatch(world: Any, monkeypatch: Any) -> None:
    from threading import Event
    from time import monotonic, sleep
    from unittest.mock import patch

    import apps.base_tap.v2 as service
    from apps.arena.models import EconomicEpoch

    player, _, state, _, season = world
    epoch = EconomicEpoch.objects.get(season=season, epoch_number=1)
    entered, release, freezer_started = Event(), Event(), Event()
    freezer_pid: list[int] = []
    real_settle = settle_player_reward

    def paused(**kwargs: Any) -> Any:
        entered.set()
        assert release.wait(10)
        return real_settle(**kwargs)

    def writer() -> None:
        connections.close_all()
        try:
            submit_batch(player, batch(state), at=AT)
        finally:
            connections.close_all()

    def freezer() -> None:
        from django.db import transaction

        connections.close_all()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    freezer_pid.append(cursor.fetchone()[0])
                freezer_started.set()
                EconomicEpoch.objects.select_for_update().get(pk=epoch.pk)
        finally:
            connections.close_all()

    with (
        patch.object(service, "settle_player_reward", side_effect=paused),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        writing = pool.submit(writer)
        assert entered.wait(5)
        freezing = pool.submit(freezer)
        assert freezer_started.wait(5)
        try:
            blocked = False
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT cardinality(pg_blocking_pids(%s))", [freezer_pid[0]])
                    blocked = cursor.fetchone()[0] > 0
                if blocked:
                    break
                sleep(0.01)
            assert blocked, "Epoch freeze must await microbatch COMMIT"
        finally:
            release.set()
        writing.result(timeout=10)
        freezing.result(timeout=10)
    assert PerformanceSourceFact.objects.count() == RewardLedger.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "points,level,stage",
    [
        (299, 2, "newborn"),
        (2499, 5, "explorer"),
        (11999, 10, "connector"),
        (29999, 15, "specialist"),
        (59799, 20, "vanguard"),
        (199999, 30, "legend"),
    ],
)
def test_level_and_dexo_boundaries(world: Any, points: int, level: int, stage: str) -> None:
    player, _, state, _, _ = world
    settle_player_reward(
        player=player,
        source_type="TEST",
        source_id="prior",
        context="TEST",
        amount=points,
        idempotency_key="prior",
        occurred_at=AT,
        resolved_config=resolve_effective_game_config(AT),
        create_settlement_outbox=False,
    )
    result = submit_batch(player, batch(state), at=AT)
    progression = result["snapshot"]["progression"]
    assert progression["lifetime_points"] == points + 1 and progression["level"] == level
    assert progression["dexo_stage"] == stage and progression["points_into_level"] == 0


@pytest.mark.django_db
def test_season_rollover_preserves_points_and_fences_old_lease(
    world: Any, close_season_for_test: Any, season_factory: Any
) -> None:
    player, session, state, config, season = world
    original = batch(state, count=5)
    submit_batch(player, original, at=AT)
    close_season_for_test(season)
    later = season.ends_at + timedelta(days=1)
    replacement = season_factory(config, starts_at=later)
    with error("EPOCH_FINALIZED"):
        submit_batch(player, batch(state, start=6, key="old-season"), at=later)
    fresh = issue_lease(player, session=session, at=later)
    assert fresh["season_id"] == replacement.code and fresh["burst_remaining"] == 50
    assert fresh["season_points"] == 0 and fresh["progression"]["lifetime_points"] == 5
    replay = submit_batch(player, original, at=later)
    assert replay["duplicate"] and replay["snapshot"]["season_id"] == replacement.code
    assert PlayerSeason.objects.get(player=player, season=season).season_points == 5


def test_committed_frontend_fixtures_validate() -> None:
    from apps.base_tap.schemas import LeaseInput, TapErrorOutput

    fixture = json.loads(
        (Path(__file__).resolve().parents[3] / "docs/fixtures/tap-v2.json").read_text()
    )
    LeaseInput.model_validate(fixture["lease_request"])
    for key in ("batch_request", "partial_request"):
        BatchInput.model_validate(fixture[key])
    for key in ("initial_state", "state_after_daily_reward"):
        TapStateOutput.model_validate(fixture[key])
    for key in (
        "batch_response",
        "duplicate_response",
        "partial_response",
        "recovered_previous_lease_response",
        "partial_recovery_response",
    ):
        BatchOutput.model_validate(fixture[key])
    for response in fixture["errors"]:
        TapErrorOutput.model_validate(response)


@pytest.mark.django_db
def test_existing_historical_projection_150_is_never_reset(world: Any) -> None:
    player, _, state, config, season = world
    PlayerProgression.objects.create(
        player=player, lifetime_points=150, level=1, dexo_stage="newborn", config_version=config
    )
    PlayerSeason.objects.create(player=player, season=season, season_points=150)
    result = submit_batch(player, batch(state, count=50), at=AT)
    assert result["snapshot"]["progression"]["lifetime_points"] == 200
    assert result["snapshot"]["season_points"] == 200
    assert RewardLedger.objects.get().player_amount == 50
