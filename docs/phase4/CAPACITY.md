# Measured capacity and remaining validation

These measurements do **not** certify million-player Production readiness. A direct one-million
registered-player run was feasible at the normalized economic receipt boundary. It is not a
one-million-weekly-active run. Full source-producer throughput, realistic concurrent traffic,
large correction cohorts and large Season Close remain unmeasured.

## Environment and method

Disposable PostgreSQL 17.11, Redis 8 and RabbitMQ 4.3; Python 3.13; three available CPUs and an
8 GiB managed container limit. PostgreSQL data is on tmpfs, so disk/WAL latency is optimistic
relative to a durable Production database. No deployed database or real users were used.
The opt-in `test_phase4_scale.py` fixture deterministically creates registered/inactive/flagged
accounts and normalized immutable recognition receipts. The actual Prepare, qualification,
readiness, Freeze, snapshot, allocation, settlement and reconciliation services execute on PostgreSQL.
Source-producer throughput is explicitly outside this benchmark boundary. Small functional tests
exercise real Typing/Mission/Clan/Creator/referral producers separately.

Reproduce in an explicitly disposable local environment with the documented test settings:

```sh
POSTGRES_DB=phase4_capacity python scripts/benchmark_phase4.py --scales 1000,10000,50000,100000
POSTGRES_DB=phase4_million python scripts/benchmark_phase4.py --scales 1000000
POSTGRES_DB=phase4_mixed python scripts/benchmark_phase4.py --scales 10000,50000 --profile mixed
POSTGRES_DB=phase4_concentrated python scripts/benchmark_phase4.py --scales 10000 --profile concentrated
POSTGRES_DB=phase4_concurrent python scripts/benchmark_phase4.py --scales 10000 --concurrent-gameplay
```

Set POSTGRES_HOST=127.0.0.1 and isolated PostgreSQL credentials, Redis and broker URLs first;
use the repository virtualenv interpreter. The launcher refuses nonlocal/non-`phase4_*` databases.
Pytest creates its separate `test_*` database. These expensive probes are skipped in routine CI.
`config/benchmarks/phase4.disposable.compose.yaml` reproduces localhost-only services under a
separate Compose project: `docker compose -p phase4-disposable -f
config/benchmarks/phase4.disposable.compose.yaml up -d`. Credentials are public test-only values;
PostgreSQL uses tmpfs. Never attach an existing application volume. Export
DJANGO_SETTINGS_MODULE=dexo_land.settings.test, POSTGRES_USER=phase4,
POSTGRES_PASSWORD=phase4_test_only, POSTGRES_HOST=127.0.0.1 and POSTGRES_PORT=15432.
REDIS_URL=redis://127.0.0.1:16379/0 and
CELERY_BROKER_URL=amqp://phase4:phase4_test_only@127.0.0.1:15672// complete the test environment.
JSON artifacts include stage times/query counts, process CPU/peak RSS, database counters/size,
WAL positions and EXPLAIN ANALYZE BUFFERS. Final million-run application hashes identify the code
measured. Smaller runs preceded final replay/inspection improvements; treat them as a scaling
series, not repeated statistical samples of identical head code.

## Observations

| Registered | Active / qualified | Normalized receipts | Profile | Economic seconds | Python peak RSS KiB |
| ---: | ---: | ---: | --- | ---: | ---: |
| 1,000 | 100 | 200 | sparse Typing | 0.955 | 166,764 |
| 10,000 | 1,000 | 2,000 | sparse Typing | 6.822 | 166,764 |
| 50,000 | 5,000 | 10,000 | sparse Typing | 34.537 | 250,224 |
| 100,000 | 10,000 | 20,000 | sparse Typing | 65.530 | 363,012 |
| 1,000,000 | 100,000 | 200,000 | sparse Typing, first run | 536.500 | 2,454,580 |
| 1,000,000 | 100,000 | 200,000 | sparse Typing, final-code repeat | 544.735 | 2,463,240 |
| 10,000 | 2,500 | 12,793 | mixed Clan/Creator/referral/Typing | 10.359 | 190,808 |
| 50,000 | 12,500 | 63,959 | mixed | 53.212 | 388,100 |
| 10,000 | 5,000 | 29,980 | concentrated Creator/copied content | 26.261 | 245,216 |
| 10,000 | 1,000 | 2,000 | sparse + one concurrent Daily claim | 6.884 | 169,808 |

The repeat million-run artifact is `performance/1000000.json`; its exact observations supersede
the first-run row above. Retained first-run measurements are in `1000000-first-run.json`.
The final-code repeat passed in 694.16 seconds including pytest setup/teardown; measured fixture
and pipeline wall time was 669.301s, Python CPU 515.539s and player seeding 87.901s. It recorded
hashes of the economic implementation at entry and asserted they were unchanged at completion.

The first million run took 677.218 seconds including fixture creation, 95.679 seconds to create
players, and 524.938 Python CPU seconds. Economic stages: Prepare/qualification/manifest 274.430s
(97,072 SQL calls), readiness 69.629s (50), Freeze 174.387s (609), settlement 18.043s (57),
reconciliation 0.011s (7). Scoped flagged-account deferral work still drives many Prepare queries.
Database size grew from approximately 19.47 MB to 1.423 GB, including synthetic setup and indexes;
this is not the isolated cost of a normal Epoch. Asynchronous pg_stat_database counters reported
about 20.5M additional returned tuples, 15.9M fetched tuples, 2.73M inserted tuples and 199 MB of
temporary data. Counter snapshots are approximate, not per-stage exact row accounting.

The recognition plan read all 200,000 matching receipts through the primary-key index in ID order
(102.657ms including execution); there were no other Epochs to filter. This does not establish
index selectivity across many Seasons. The existing Epoch index is available, but future validation
must include realistic historical table growth and planner statistics. Some full-cohort scans are
necessary for deterministic allocation; repeatedly materializing full source/player structures is
still a memory bottleneck despite compact manifests and batched writes.

The concurrent Daily claim completed successfully but waited 3.394 seconds during Prepare at
only 10,000 registered players. Exclusive Season barriers remain held for substantial Prepare and
Freeze transactions. At one million registrations the observed stages last minutes, so interactive
latency is an unresolved constraint. PostgreSQL reported no deadlocks in the synthetic runs; that
does not establish a contention failure rate under traffic. A sampled database container used
2.432 GiB memory and roughly one CPU; this is a sample, not peak instrumentation.

WAL positions are cluster-global and overlapped other disposable tests. Do not interpret their
delta as isolated write amplification. tmpfs block-I/O counters cannot predict durable storage.
No sustained arrival-rate/queue test, worker throughput curve, lock-wait distribution or measured
backlog/catch-up SLA is claimed. Same-Season concurrent workers are deliberately serialized;
small PostgreSQL concurrency/crash tests verify safety, not throughput across Seasons.

## Bounded planning scenarios, not readiness claims

For **normalized** input and this hardware, use these provisional budgets for a million registered
accounts. They are conditional planning envelopes rather than statistical confidence intervals:

| Scenario | Assumptions | Epoch service time | Application memory planning |
| --- | --- | --- | --- |
| optimistic | 10% active, two receipts each, warm tmpfs-like DB | 8–12 minutes, anchored by direct run | 3–4 GiB per economic worker |
| base | 25% active, mixed receipts; observed 50k mixed/sparse ratio ~1.54 plus contention headroom | 14–25 minutes | 6–8 GiB per worker; unverified |
| conservative | 50% active, concentrated Creator receipts; observed 10k ratio ~3.85 plus spill/lock overhead | 35–70+ minutes | 10–16+ GiB per worker; unverified |

Only the sparse scenario was directly measured at one million registrations. The mixed and
concentrated envelopes combine observed workload ratios, growing memory and possible spilling;
they are unreliable once source fan-out, historical table volume or contention differs materially.
Real producer work, corrections, Close and queue delays are additional, not hidden in these times.
No claim is made for one million weekly active or qualified accounts.

Next validation tier: one million registered / 250k active mixed population on dedicated durable
PostgreSQL, followed by 500k active concentrated/abusive traffic. Start with at least 8 vCPU/32 GiB
database resources, a separate 4–8 vCPU/16 GiB economic worker and a separate latency-sensitive
gameplay pool; these are proposed test resources, not validated Production sizing. Use session
pooling/direct connections, explicit memory/WAL budgets and queues isolated from webhook traffic.
Measure source production, full correction reconstruction/Close, p95/p99 gameplay latency, waits,
WAL/checkpoints, crash catch-up and history-heavy plans before approving capacity.

Likely next engineering work: durable chunked preparation/cohort staging with an integrity barrier
at sealing, batched scoped deferrals, bounded scoring inputs and manifests, history-selective indexes
proven by plans, and worker resource isolation. These require further correctness design; this PR
does not weaken barriers or change economic outcomes to improve the benchmark.
