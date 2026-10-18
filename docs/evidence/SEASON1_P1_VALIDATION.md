# Phase 1 freeze safety validation

Base main independently fetched and inspected: `7a9f460378ce27c927d90de3ec1bf7e9d5a1bfaf`.
This is isolated local evidence, not production verification or policy approval.

## Reproduction before implementation

Command: `uv run pytest apps/arena/tests/test_freeze_readiness.py -q`.

```
test_missing_qualification_completion_cannot_freeze
E Failed: DID NOT RAISE <class 'apps.arena.epochs.EpochBlocked'>
1 failed in 11.96s
```

The assertion expected a blocker before any final budgets/snapshots/timestamps. Baseline Freeze
instead completed with no qualification evaluation. The test was retained and passes after the fix.

## Environment and executed checks

Dedicated, newly created local PostgreSQL 17, Redis 8 and RabbitMQ 4.3 containers, separate from the
workspace's existing application services. Python 3.13.16, locked `uv sync --frozen`. Tests use
`dexo_land.settings.test`; no production database, credentials or Telegram endpoint was used.

- `uv sync --frozen`: passed; lock/dependency files unchanged.
- `uv run ruff check .`, `uv run ruff format --check .`: passed.
- `uv run mypy .`: passed, 468 source files.
- `uv run python manage.py check`: passed, zero issues.
- `uv run python scripts/validate_contract.py`: passed.
- `uv run python scripts/export_{tap_v2,b3,b4,b5,b6}_openapi.py --check`: all five passed
  (the brace expression denotes the separately executed script names).
- `uv run python manage.py migrate --noinput`, `makemigrations --check --dry-run`,
  `migrate --check`: passed; no schema drift.
- Focused `uv run pytest apps/arena/tests/test_freeze_readiness.py apps/base_tap/tests/test_v2.py -q`:
  **55 passed**. Positive-policy/source-completion cases use a clearly labelled synthetic verifier;
  this evidence does not establish a valid production qualification predicate or ingress seal.
- Full backend suite, opt-in benchmark and hosted Actions outcomes are recorded on the Draft PR
  and final handoff at the exact delivered commit. Earlier intermediate failing runs were corrected,
  not skipped or treated as acceptance.

## PostgreSQL concurrency and rollback evidence

The focused suite uses actual transactions, advisory locks, database triggers and separate thread
connections for concurrent Freeze workers, canonical shared writer vs exclusive Freeze, qualification
vs Freeze, failed evaluation and process-loss resume. A real PostgreSQL division-by-zero on the second
snapshot insert proves rollback of both prior snapshot and category budgets; retry creates exactly
five snapshots and five budgets. Concurrent repeated empty Freeze creates one set of five budgets.
Expected not-ready tasks return zero repeatedly, preserve DRAINING, and create no evaluations or DLX.

Tests also cover missing/partial/failed/RUNNING evaluations, explicit synthetic empty completion,
wrong pinned config, stale decisions, moderation, exact policy version/revocation, pending/late
Telegram/dead-letter ingestion, Network boundary, Clan windows/signals, Base Tap exact-once/boundary,
Typing/referral durable source-time preservation, and a READ ONLY preflight with a SQL write guard.
Existing economic/immutability assertions remain; legacy setup now explicitly supplies synthetic
readiness rather than bypassing the new database guard.

## Migration preservation rehearsal

A second database, `wedcal_p1_migrations`, was migrated to the full schema and then **only in this
isolated database** reverted to Arena 0010 before creating representative historical records.
Synthetic data included six Epochs, one already FROZEN Epoch/snapshot, one QualificationDecision,
a durable Tap source fact and RewardLedger event with PlayerSeason/progression, and published config.
0011/0012 were then applied forward. Count plus SHA-256 over every JSON row in all eight populated
history/config tables matched byte for byte. No new evaluation was backfilled and no historical
frozen state/config/source timestamp was changed. Migration consistency checks passed again.

- [Rendered schema SQL](SEASON1_P1_SCHEMA.sql)
- [Rendered trigger SQL](SEASON1_P1_GUARDS.sql)
- [Identical before/after counts and checksums](SEASON1_P1_MIGRATION_PRESERVATION.json)

0011 creates a new empty table/index/constraints. 0012 installs guards and requires brief DDL locks
on Epoch/evaluation tables. No unbounded data migration or economic rewrite exists. Production lock
wait/duration still needs staging/workload observation; isolated success is not a production duration
estimate. Reversing the guards would remove protection and is not a recommended production rollback.

No merge, deployment, production migration/write, operator hold, live task dispatch, or Phase 2/3
work occurred. See the [operator/design handoff](../SEASON1_PHASE1_FREEZE_SAFETY.md) for the unresolved
policy/source decisions and exact Docker Compose read-only inspection procedures.
