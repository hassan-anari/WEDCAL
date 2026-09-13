# Phase 11 Season-close and production operations

> **Stage 1 — implementation complete / verification pending.** Every capacity, recovery,
> restore, security, and SLO result below is **MEASUREMENT REQUIRED IN STAGE 2**.

## Season close

Season 1 earns for `[starts_at, starts_at + 6 × 7 days)`. The final three days are a close and
reconciliation window and never form epoch seven. Eligibility always follows persisted source time,
not worker time.

1. Preview without mutation: `python manage.py season_close founding-season`.
2. Resolve every named blocker. RabbitMQ depth is diagnostic only; PostgreSQL providers are the
   durable barrier. Never manually edit `Season.status`.
3. Start/resume: `python manage.py season_close founding-season --start --actor OPERATOR --reason
   "Day-42 scheduled close" --ticket TICKET-ID`.
4. Monitor `SeasonCloseRun` in Admin and `/operations/metrics` with `X-Operations-Token`.
5. Stages are PREPARING, DRAINING, RECONCILING, FREEZING_RANKINGS, AWARDING_HONORS, FINALIZING,
   COMPLETED. FAILED retains its failing stage/category and resume continues forward.
6. Use `python manage.py reconcile_season founding-season` for an independent dry-run. XP repair is
   limited to the projection: `--repair-xp` never edits `RewardLedger` or lifetime progression.

### Finalization barrier and retry contract

All close-relevant writers take the shared PostgreSQL Season advisory transaction lock before their
domain row locks. Close takes the exclusive form only after an initial drain, then repeats every
durable provider query under that lock before persisting `barrier_established_at`. Once persisted,
shared writers fail closed; already-created idempotent ledger retries may still return their immutable
result. Canonical order is **Season advisory barrier → Player/Season/domain rows → append-only ledger
or projection → commit**. RabbitMQ state never participates.

A stage failure rolls back its stage transaction. The exception is caught outside that transaction;
a new transaction locks the close run and durably records FAILED, the exact failed stage/category,
timestamp, and cleared scheduling metadata before the exception is re-raised. Operator resume restores
that exact stage and increments the retry count once. Transient drain blockers persist `TRANSIENT` and
a deployment-configured delayed recheck timestamp; manual-review blockers persist `MANUAL`, schedule
no polling, and wait for explicit operator resolution/resume.

XP repair is bounded by a persisted player cursor and delegates to
`rebuild_player_reward_projections`, which rebuilds Lifetime, level, Dexo stage, every Season score,
and tie timestamp from immutable RewardLedger history. Referral drain considers only configured
share-eligible RewardLedger sources in the closing Season without a canonical outcome. DLX adjustment
reporting uses amounts—not row counts—and reports held/released/forfeited/reversed totals. Resolved
epoch anomalies remain immutable; use `python manage.py resolve_epoch_anomaly ...` with the dedicated
permission and explicit disposition.

Close auto-settles source-Season Typing credits and final server-owned `REWARD_READY` mission
entitlements. It freezes immutable PostgreSQL Player/Clan standings, evaluates zero-point medals
with integer rule `rank × 10000 <= population × percentile_bps`, records category residuals without
redistribution, ends ClanSeason earning, and only then transitions Season to CLOSED. Rollback of
economic history is forbidden: stop dispatch, preserve the run, investigate, and forward-fix using
an audited correction. Escalate unresolved anomalies, holds, unsafe invites, or reconciliation
drift to product/security/economy owners.

`read-season-brief` has no approved server-owned completion event. **PH-043 IMPLEMENTATION BLOCKED
BY PRODUCT DECISION**; do not accept a client boolean or auto-complete it.

## Security and secrets

Production requires explicit allowed hosts, CSRF trusted origins, operations token, DB/Redis/Rabbit
URLs, Telegram secrets, secure cookies and DEBUG=false. Set `DJANGO_TRUST_X_FORWARDED_PROTO=true`
only where a trusted edge strips and sets that header. Restrict `/admin` and `/operations/metrics`
at the edge. Terminate TLS there and approve HSTS only after the complete hostname/TLS topology is
verified.

Rotate secrets one family at a time: create a replacement in the deployment secret store, update
consumers, rolling-restart API/worker/Beat, verify health and lag, then revoke the old value. For the
webhook secret, install the new Telegram webhook immediately after deployment. For bot-token
rotation, pause outbound tasks during token replacement. Rotate Django secret with awareness that
sessions/signatures become invalid. PostgreSQL/RabbitMQ/Redis rotation must overlap credentials
where supported, update URLs, restart clients, verify, then revoke. Never place values in Git or
logs.

## Backup and restore

Create: `DATABASE_URL=... BACKUP_OUTPUT=/secure/path/file.dump scripts/backup_postgres.sh`.
Restore only to an isolated target:

```bash
RESTORE_DATABASE_URL=... BACKUP_INPUT=/secure/path/file.dump \
ALLOW_DESTRUCTIVE_ISOLATED_RESTORE=YES_ISOLATED_TARGET scripts/restore_postgres.sh
DJANGO_SETTINGS_MODULE=... python manage.py migrate --check
python manage.py production_data_report
python manage.py reconcile_season founding-season
```

The Stage-2 restore comparison must cover RewardLedger, Season/PlayerSeason/lifetime progression,
referrals, Clan ledgers/projections, six epochs, DLX allocations/adjustments/residuals, final
standings, and moderation evidence metadata. Record checksums/counts, backup freshness, RPO/RTO.
No restore was executed in Stage 1.

## Redis, RabbitMQ, workers, and Beat

Redis loss: restore service, run existing Arena ranking rebuild tasks; CLOSED Seasons rebuild from
immutable final standings. Never use Redis as an economic recovery source. RabbitMQ loss: restore
broker, restart workers, then invoke bounded redrive/Beat tasks; durable ingress/outboxes and leases
are truth. Workers use late ACK, worker-lost rejection, prefetch 1 and bounded time limits. The
`run_beat_singleton` command holds a PostgreSQL advisory lock, mechanically preventing concurrent
Beat schedulers.

## Performance, queries, monitoring, and sizing

Generate manifests only (not data):
`python scripts/stage2_load_plan.py --users 50000 --seed 11 --scenario season_close`. Supported
tiers are 50K/100K/250K/500K/1M and scenarios cover API reads, bootstrap, Base Tap, Telegram,
activity, outboxes, Creator, Referral, ranking, weekly DLX, and close. Use
`python manage.py explain_query FAMILY --season founding-season` and
`python manage.py production_data_report` for repeatable evidence.

Stage 2 must populate: API and webhook p95/p99, queue/provider oldest ages, close/epoch duration,
DB CPU/IO/locks, Redis memory, Rabbit throughput, worker concurrency/RSS, backup RPO/RTO, cleanup
backlog, and alert thresholds. **MEASUREMENT REQUIRED IN STAGE 2.** GameConfig cutover must use the
existing publication preview/control workflow and preserve source config provenance.

## Branch protection and release

Repository owner action is required: protect `main`, require PRs plus Contract Gate and Foundation,
block force pushes/deletion, and document a time-limited audited emergency bypass. Run the Phase-11
dependency/container scan workflow and review artifacts; its container scan is report-only until an
owner-approved severity policy exists.
