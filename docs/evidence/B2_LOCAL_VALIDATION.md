# B2 local validation evidence (2026-10-14)

The [same-PR compatibility follow-up](../B2_CONTRACT_COMPATIBILITY_FOLLOWUP.md) records the newer 680-test regression and [new benchmark](b2-followup-benchmark.json); the original measurements below are retained as historical evidence.

Base main `95f8458e3058112e12053d090f54a10e0aedd599`. Evidence is synthetic/local; no public endpoint traffic, deployed user inventory, production migration or VPN-off claim.

- Full repository regression: **659 passed**, one opt-in benchmark skipped, one existing static-directory warning, 98.53s. Final B2/Avatar/fixture rerun: **39 passed**, 14.23s (includes the additional historical projection-only 150-point preservation case). Hosted CI tests final head separately.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`: green; 358 Python sources typed.
- `manage.py check`, `makemigrations --check --dry-run`, `scripts/validate_contract.py`: green, no migration drift. New migration graph: additive base_tap.0002, players.0005, concurrent rewards.0006. No static-fix DB migration.
- Docker build/run: production-compatible manifest, 127 collected/354 postprocessed files, manifest **10404 bytes**, Admin login **200**, `/static/admin/css/base.96c479cedf7a.css` **200**, DEBUG=False, UID **999**. Deterministic build failure/smoke and CI/deploy checks committed. Image build logs remained local scratch; no secret extraction.
- [Legacy reconciliation JSON](legacy-drain.json): dry-run sees valid 25 XP pending PROCESSING plus already committed original ledger; apply changes pending→0/SETTLED without a second credit. Test asserts progression/ledger remain 25. Other real concurrent tests prove four finalizers credit one obligation exactly once.
- Real PostgreSQL epoch-exclusive lock waits behind active batch shared lock; direct `pg_blocking_pids` proof. Real two-device/duplicate microbatch races, all Level/Dexo thresholds, moderation/rollback, cooldown 9.9/10s, same-day unlimited bursts, UTC/epoch/Season/config boundaries and B1/Daily/Missions/Typing/Clan/Season regressions.

## Final PostgreSQL service benchmark

Reproduce **alone**: `B2_BENCHMARK=1 uv run pytest apps/base_tap/tests/test_benchmark.py -q`. A required CI step reruns it after the full suite and uploads raw JSON. Never run two pytest processes against the same test database.

PostgreSQL **17.11**, 64 synthetic players, 16 workers, four bursts/player, 25 taps/batch. 512 accepted batches + 256 lease requests in **21.092s**; **24.274 accepted batches/s**, **36.411 total service operations/s**. Batch p50/p95/p99 **497.05/676.624/992.308ms**. Lease p50/p95/p99 **254.068/371.919/508.678ms**. Player-lock acquisition p50/p95/p99 **8.02/25.342/35.898ms**.

Separate same-player storm: 16 concurrent identical requests → **one** new reward and **15** duplicate receipts. Actual PostgreSQL sampler observed **15 simultaneous lock waiters**, 22 of 28 samples with waits; request p95 **345.508ms**. No waits sampled for separate-player normal profile.

Final ledger/fact/ranking rows: 513 each (512 normal + one contention reward); 257 leases/65 states include the initial fixture. PostgreSQL insert/update/delete and total relation bytes are in [raw evidence](b2-benchmark.json). Per accepted batch: one append-only ledger, source fact and ranking event, plus progression/Season/burst updates; zero extra v2 receipt rows. Table stats are cumulative in this isolated test DB including setup, not deployed table measurements.

Limits: service-level measurement excludes HTTP/auth/network, uses virtual deterministic cooldown timestamps and a small warm DB/container. It measures DB/application capacity, not real-time human reward velocity or public capacity. Remaining proven contention is serialization on a single player; durable write/index amplification and synchronous ORM/config/snapshot round trips require further capacity profiling before launch. No invented 50K/1M throughput or latency SLO. Owner must review inflation and scale/retention before activation.

## Avatar

Disabled/fallback by default. Private S3 adapter, Bot API evidence, strict source/path/redirect/IP/MIME/size/time/pixel checks, reencoding/privacy, refresh/account change/removal/expiry/storage failure and task discovery tested. No real credentials/storage or filtered-network delivery proof: exact operational prerequisites in [handoff](../B2_BACKEND_HANDOFF.md).
