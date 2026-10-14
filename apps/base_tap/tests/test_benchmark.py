"""Opt-in isolated PostgreSQL benchmark. Never uses public endpoints or existing user data."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event, Thread
from time import perf_counter
from typing import Any
from uuid import uuid4

import pytest
from django.db import connection, connections

from apps.arena.models import PerformanceSourceFact
from apps.base_tap.models import TapBurstState, TapLease
from apps.base_tap.tests.test_v2 import AT, batch, world  # noqa: F401
from apps.base_tap.v2 import issue_lease, submit_batch
from apps.rewards.models import RewardLedger


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * percent))], 3)


@pytest.mark.skipif(os.getenv("B2_BENCHMARK") != "1", reason="opt-in local benchmark")
@pytest.mark.django_db(transaction=True)
def test_representative_postgres_microbatch_load(
    world: Any,  # noqa: F811
    player_factory: Any,
    monkeypatch: Any,  # noqa: F811
) -> None:  # noqa: F811
    assert connection.vendor == "postgresql"
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        version = cursor.fetchone()[0]
    assert version.startswith("17.")
    players = [player_factory(800000 + i) for i in range(64)]
    locks: list[float] = []

    def metric(event: str, **kwargs: Any) -> None:
        if event == "tap_batch_committed":
            locks.append(kwargs["player_lock_ms"])

    monkeypatch.setattr("apps.base_tap.v2.logger.info", metric)
    stop = Event()
    wait_samples: list[int] = []

    def sample_waits() -> None:
        connections.close_all()
        try:
            while not stop.wait(0.01):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name='b2-benchmark' AND wait_event_type='Lock'"
                    )
                    wait_samples.append(cursor.fetchone()[0])
        finally:
            connections.close_all()

    def run(player: Any) -> tuple[list[float], list[float]]:
        connections.close_all()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET application_name='b2-benchmark'")
            lease_latencies: list[float] = []
            batch_latencies: list[float] = []
            session = uuid4()
            for burst in range(4):
                at = AT + timedelta(seconds=burst * 11)
                start = perf_counter()
                state = issue_lease(player, session=session, at=at)
                lease_latencies.append((perf_counter() - start) * 1000)
                for part in range(2):
                    start = perf_counter()
                    submit_batch(
                        player,
                        batch(state, count=25, start=part * 25 + 1, key=f"{burst}-{part}"),
                        at=at + timedelta(milliseconds=part * 100),
                    )
                    batch_latencies.append((perf_counter() - start) * 1000)
            return lease_latencies, batch_latencies
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_stat_force_next_flush()")
            connections.close_all()

    observer = Thread(target=sample_waits)
    observer.start()
    started = perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(run, players))
    finally:
        stop.set()
        observer.join()
    elapsed = perf_counter() - started
    leases = [x for row in results for x in row[0]]
    batches = [x for row in results for x in row[1]]
    normal_wait_samples = list(wait_samples)
    wait_samples.clear()
    stop.clear()
    observer = Thread(target=sample_waits)
    observer.start()
    hot_player, _, hot_state, _, _ = world
    gate = Barrier(16)

    def hot(_: int) -> tuple[float, bool]:
        connections.close_all()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET application_name='b2-benchmark'")
            gate.wait(timeout=10)
            start = perf_counter()
            result = submit_batch(
                hot_player, batch(hot_state, count=25, key="hot-duplicate"), at=AT
            )
            return (perf_counter() - start) * 1000, result["duplicate"]
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_stat_force_next_flush()")
            connections.close_all()

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            hot_results = list(pool.map(hot, range(16)))
    finally:
        stop.set()
        observer.join()
    assert sum(x[1] for x in hot_results) == 15
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_stat_clear_snapshot()")
        cursor.execute(
            "SELECT relname,n_tup_ins,n_tup_upd,n_tup_del,pg_total_relation_size(relid) "
            "FROM pg_stat_user_tables WHERE relname IN ('base_tap_tapburststate',"
            "'base_tap_taplease','rewards_rewardledger','arena_performancesourcefact',"
            "'arena_rankingprojectionevent','progression_playerprogression',"
            "'seasons_playerseason') ORDER BY relname"
        )
        stats = [
            {
                "table": row[0],
                "inserts_cumulative": row[1],
                "updates_cumulative": row[2],
                "deletes_cumulative": row[3],
                "total_bytes": row[4],
            }
            for row in cursor.fetchall()
        ]
    report = {
        "scope": "local PostgreSQL benchmark; auth/HTTP/network excluded",
        "postgres_version": version,
        "concurrent_workers": 16,
        "synthetic_players": 64,
        "bursts_per_player": 4,
        "taps_per_batch": 25,
        "accepted_batches": len(batches),
        "lease_requests": len(leases),
        "accepted_taps": 12800,
        "wall_seconds": round(elapsed, 3),
        "accepted_batches_per_second": round(len(batches) / elapsed, 3),
        "all_requests_per_second": round((len(batches) + len(leases)) / elapsed, 3),
        "batch_latency_ms": {
            k: percentile(batches, p) for k, p in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
        },
        "lease_latency_ms": {
            k: percentile(leases, p) for k, p in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
        },
        "player_lock_ms": {
            k: percentile(locks, p) for k, p in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
        },
        "lock_wait_sampling": {
            "interval_ms": 10,
            "samples": len(normal_wait_samples),
            "max_waiters": max(normal_wait_samples, default=0),
            "samples_with_waiters": sum(x > 0 for x in normal_wait_samples),
        },
        "same_player_contention": {
            "concurrent_duplicate_requests": 16,
            "new_rewards": 1,
            "duplicate_receipts": 15,
            "latency_ms": {
                k: percentile([x[0] for x in hot_results], p)
                for k, p in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
            },
            "lock_wait_samples": len(wait_samples),
            "max_waiters": max(wait_samples, default=0),
            "samples_with_waiters": sum(x > 0 for x in wait_samples),
        },
        "durable_rows": {
            "ledger": RewardLedger.objects.count(),
            "facts": PerformanceSourceFact.objects.count(),
            "leases": TapLease.objects.count(),
            "burst_states": TapBurstState.objects.count(),
        },
        "postgres_table_stats": stats,
        "limitations": [
            "virtual cooldown clock; database capacity measurement",
            "small warm database, one container, no public endpoints",
            "table write counters are cumulative for this test database, including fixture setup",
            "no launch-scale SLO claim",
        ],
    }
    assert RewardLedger.objects.count() == PerformanceSourceFact.objects.count() == 513
    Path("work").mkdir(exist_ok=True)
    Path("work/b2-benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
