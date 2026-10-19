# mypy: disable-error-code="no-untyped-def,no-untyped-call"
"""Opt-in PostgreSQL normalized-receipt capacity probe; never targets an existing database."""

import json
import os
import resource
from datetime import timedelta
from pathlib import Path
from time import perf_counter, process_time
from typing import Any

import pytest
from django.db import connection, transaction
from django.db.models import Count

from apps.arena.economics import prepare_epoch
from apps.arena.epochs import freeze_epoch, reconcile_epoch, settle_epoch
from apps.arena.models import EconomicRecognition
from apps.arena.policy import digest
from apps.arena.readiness import evaluate_epoch_readiness
from apps.players.models import Player
from apps.seasons.barrier import acquire_economic_write_barrier

from .conftest import START
from .test_phase4_configurable import configurable_world  # noqa: F401

SCALES = [int(value) for value in os.getenv("PHASE4_SCALES", "1000,10000,50000").split(",")]


@pytest.mark.skipif(
    os.getenv("PHASE4_BENCHMARK") != "1", reason="Explicit disposable PostgreSQL benchmark only"
)
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("population", SCALES)
def test_normalized_epoch_capacity(configurable_world, population):  # noqa: F811
    assert connection.settings_dict["NAME"].startswith("test_"), "Disposable pytest DB required"
    profile = os.getenv("PHASE4_ACTIVITY_PROFILE", "core_sparse")
    assert profile in {"core_sparse", "mixed", "concentrated"}
    w = configurable_world()
    epoch = w.epochs[0]
    concurrent = os.getenv("PHASE4_CONCURRENT_GAMEPLAY") == "1"
    from threading import Event

    barrier_acquired = Event()
    stages = {}
    counts = {"queries": 0}

    def count_sql(execute, sql, params, many, context):
        counts["queries"] += 1
        result = execute(sql, params, many, context)
        if concurrent and "pg_advisory_xact_lock(" in sql:
            barrier_acquired.set()
        return result

    def db_stats():
        with connection.cursor() as c:
            c.execute(
                "SELECT tup_returned,tup_fetched,tup_inserted,tup_updated,deadlocks,temp_bytes "
                "FROM pg_stat_database WHERE datname=current_database()"
            )
            values = c.fetchone()
            c.execute("SELECT pg_current_wal_lsn()::text,pg_database_size(current_database())")
            lsn, size = c.fetchone()
        return {"database_counters": values, "wal_lsn": lsn, "database_bytes": size}

    with connection.cursor() as c:
        c.execute("SELECT version()")
        version = c.fetchone()[0]
    from hashlib import sha256

    application_paths = [
        "apps/arena/economics.py",
        "apps/arena/epochs.py",
        "apps/arena/scoring.py",
        "apps/arena/policy.py",
        "apps/arena/recognition.py",
        "apps/arena/growth.py",
        "apps/arena/qualification.py",
        "apps/game_config/services.py",
        "apps/seasons/close.py",
    ]
    application_hashes = {
        path: sha256(Path(path).read_bytes()).hexdigest() for path in application_paths
    }
    before = db_stats()
    cpu = process_time()
    started = perf_counter()
    # Deterministic registered/active mix. Receipts are seeded at the normalized boundary;
    # this measures economic services, NOT source-producer or human-verification throughput.
    active = 0
    pending = []
    for i in range(population):
        pending.append(
            Player(
                telegram_user_id=2000000 + i,
                first_seen_at=START - timedelta(days=3),
                last_seen_at=START,
                first_mini_app_at=START - timedelta(days=3),
                first_seen_source="MINI_APP",
                moderation_state="FLAGGED" if i % 100 == 19 else "ACTIVE",
            )
        )
        if len(pending) == 1000:
            Player.objects.bulk_create(pending, batch_size=1000)
            pending.clear()
    Player.objects.bulk_create(pending, batch_size=1000)
    seed_seconds = perf_counter() - started
    receipts = []
    with transaction.atomic():
        acquire_economic_write_barrier(w.season.pk)
        for i, pid in enumerate(
            Player.objects.order_by("pk").values_list("pk", flat=True).iterator(chunk_size=1000)
        ):
            if i % (10 if profile == "core_sparse" else 4 if profile == "mixed" else 2) != 0:
                continue  # 90% registered inactive in this workload.
            active += 1
            for day in (0, 1):
                at = START + timedelta(days=day, hours=1)
                inputs = {"amount": 70, "quality": 50, "credit_id": i * 2 + day + 1}
                evidence = digest(
                    {
                        "season": w.season.pk,
                        "player": pid,
                        "actor": pid,
                        "source_time": at.isoformat(),
                        "config": w.config.pk,
                        "inputs": inputs,
                        "category": "CORE_GAMEPLAY",
                    }
                )
                receipts.append(
                    EconomicRecognition(
                        epoch=epoch,
                        player_id=pid,
                        actor_id=pid,
                        category="CORE_GAMEPLAY",
                        component="typing",
                        source_type="TYPING_CREDIT",
                        source_id=f"synthetic:{pid}:{day}",
                        source_time=at,
                        finalized_at=at,
                        recognized_at=at,
                        config_version=w.config,
                        policy=w.document,
                        inputs=inputs,
                        evidence_digest=evidence,
                    )
                )
            if len(receipts) >= 1000:
                EconomicRecognition.objects.bulk_create(receipts, batch_size=1000)
                receipts.clear()
        EconomicRecognition.objects.bulk_create(receipts, batch_size=1000)

    if profile != "core_sparse":
        from hashlib import sha256

        active_ids = [
            pid
            for i, pid in enumerate(Player.objects.order_by("pk").values_list("pk", flat=True))
            if i % (4 if profile == "mixed" else 2) == 0
        ]
        extra = []

        def add(pid, actor, kind, category, component, identity, when, inputs):
            proof = digest(
                {
                    "season": w.season.pk,
                    "player": pid,
                    "actor": actor,
                    "source_time": when.isoformat(),
                    "config": w.config.pk,
                    "inputs": inputs,
                    "category": category,
                }
            )
            extra.append(
                EconomicRecognition(
                    epoch=epoch,
                    player_id=pid,
                    actor_id=actor,
                    category=category,
                    component=component,
                    source_type=kind,
                    source_id=identity,
                    source_time=when,
                    finalized_at=when,
                    recognized_at=when,
                    config_version=w.config,
                    policy=w.document,
                    inputs=inputs,
                    evidence_digest=proof,
                )
            )
            if len(extra) >= 1000:
                EconomicRecognition.objects.bulk_create(extra, batch_size=1000)
                extra.clear()

        with transaction.atomic():
            acquire_economic_write_barrier(w.season.pk)
            for index, pid in enumerate(active_ids):
                for day in (0, 1):
                    when = START + timedelta(days=day, hours=2)
                    if profile == "mixed" and index % 3 == 0:
                        add(
                            pid,
                            pid,
                            "CLAN_CONTRIBUTION",
                            "CLAN_PERFORMANCE",
                            "contribution",
                            f"clan:{pid}:{day}",
                            when,
                            {"contribution": 100},
                        )
                    if profile == "mixed" and index % 10 == 0:
                        for actor in active_ids[index + 1 : index + 4]:
                            for slot in range(4):
                                at = when + timedelta(seconds=slot * 2700)
                                add(
                                    pid,
                                    actor,
                                    "CREATOR_ACTIVITY",
                                    "CREATOR_COMMUNITY_GROWTH",
                                    "speaker",
                                    f"creator:{pid}:{actor}:{day}:{slot}",
                                    at,
                                    {
                                        "fingerprint": sha256(
                                            f"{pid}:{actor}:{day}:{slot}".encode()
                                        ).hexdigest(),
                                        "window": day * 100 + slot,
                                        "source_seconds": day * 86400 + 7200 + slot * 2700,
                                        "characters": 200,
                                    },
                                )
                    if profile == "concentrated":
                        owner = active_ids[index % 5]
                        if owner != pid:
                            for slot in (0, 1):
                                at = when + timedelta(seconds=slot * 2700)
                                add(
                                    owner,
                                    pid,
                                    "CREATOR_ACTIVITY",
                                    "CREATOR_COMMUNITY_GROWTH",
                                    "speaker",
                                    f"concentrated:{pid}:{day}:{slot}",
                                    at,
                                    {
                                        "fingerprint": sha256(
                                            f"copied-template:{slot}".encode()
                                        ).hexdigest(),
                                        "window": day * 100 + slot,
                                        "source_seconds": day * 86400 + 7200 + slot * 2700,
                                        "characters": 200,
                                    },
                                )
                if profile == "mixed" and index % 20 == 0:
                    add(
                        pid,
                        active_ids[(index + 1) % len(active_ids)],
                        "REFERRAL_ACTIVATION",
                        "VERIFIED_REFERRAL",
                        "acquisition",
                        f"acquisition:{pid}",
                        when,
                        {"invitee_id": active_ids[(index + 1) % len(active_ids)]},
                    )
            EconomicRecognition.objects.bulk_create(extra, batch_size=1000)
    w.clock["at"] = epoch.ends_at
    concurrent_result = None
    if concurrent:
        from concurrent.futures import ThreadPoolExecutor

        from django.db import close_old_connections, connections

        from apps.daily.services import claim_daily

        probe = w.player()
        w.clock["at"] = epoch.ends_at + timedelta(minutes=1)

        def gameplay():
            close_old_connections()
            try:
                assert barrier_acquired.wait(timeout=30)
                started = perf_counter()
                row, created = claim_daily(probe, w.clock["at"])
                return {
                    "seconds": perf_counter() - started,
                    "created": created,
                    "source_season_id": row.season_id,
                    "source_config_id": row.config_version_id,
                }
            finally:
                connections.close_all()

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(gameplay)
    operations = [
        ("prepare_qualification_manifest", lambda: prepare_epoch(epoch)),
        ("readiness", lambda: evaluate_epoch_readiness(epoch, evaluation_key="scale")),
        ("freeze_snapshots", lambda: freeze_epoch(epoch)),
        ("settlement", lambda: settle_epoch(epoch)),
        ("reconciliation", lambda: reconcile_epoch(epoch)),
    ]
    for label, operation in operations:
        q = counts["queries"]
        tick = perf_counter()
        with connection.execute_wrapper(count_sql):
            result: Any = operation()
        stages[label] = {"seconds": perf_counter() - tick, "queries": counts["queries"] - q}
        if label == "readiness":
            assert result.status == "COMPLETE", result.blocking_reasons
        if label == "reconciliation":
            assert all(result.values())
    if concurrent:
        concurrent_result = future.result(timeout=30)
        pool.shutdown()
        assert concurrent_result["created"]
    assert all(
        sha256(Path(path).read_bytes()).hexdigest() == value
        for path, value in application_hashes.items()
    )
    after = db_stats()
    with connection.cursor() as c:
        c.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * "
            "FROM arena_economicrecognition WHERE epoch_id=%s ORDER BY id",
            [epoch.pk],
        )
        plan = c.fetchone()[0]
    output = {
        "application_hashes": application_hashes,
        "registered": population,
        "concurrent_gameplay": concurrent_result,
        "active": active,
        "qualified": epoch.performance_snapshots.values("player_id").distinct().count(),
        "normalized_receipts": EconomicRecognition.objects.filter(epoch=epoch).count(),
        "profile": profile,
        "source_producer_throughput_measured": False,
        "mix": "90% inactive, 10% typing"
        if profile == "core_sparse"
        else "25% active, Clan/Creator/acquisition receipts"
        if profile == "mixed"
        else "50% active, concentrated Creator owners and copied content",
        "source_mix": list(
            EconomicRecognition.objects.filter(epoch=epoch)
            .values("source_type")
            .annotate(count=Count("pk"))
        ),
        "postgres": version,
        "seed_players_seconds": seed_seconds,
        "wall_seconds_including_seed": perf_counter() - started,
        "economic_seconds": sum(v["seconds"] for v in stages.values()),
        "stages": stages,
        "python_cpu_seconds": process_time() - cpu,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "before": before,
        "after": after,
        "recognition_query_plan": plan,
        "limitations": [
            "not a gameplay traffic benchmark",
            "raw Creator/Clan/referral producer throughput excluded",
            "no external Telegram calls",
            "one economic worker, no concurrent gameplay load",
        ],
    }
    directory = Path("work/phase4-scale")
    directory.mkdir(parents=True, exist_ok=True)
    (
        directory
        / (
            f"{population}-concurrent.json"
            if concurrent
            else f"{population}.json"
            if profile == "core_sparse"
            else f"{population}-{profile}.json"
        )
    ).write_text(json.dumps(output, indent=2, default=str) + "\n")
