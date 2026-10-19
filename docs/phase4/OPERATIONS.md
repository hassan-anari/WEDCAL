# Weekly economics activation and pause runbook

This PR is development-only. Do not run these activation examples against Production without
separate owner authorization. The PR does not activate any Season, policy or DLX worker.
The owner addendum approves the generic pre-Freeze rule; no live policy or activation is approved. Shipped normal policies disable Growth.

1. Review source retention, durable canonical evidence and historical coverage. Unknown global
   ingress remains a blocker; scoping unfinished user work does not assert it finished.
2. Use the [two-person registration workflow](POLICY_REGISTRATION.md) to validate, submit and
   independently approve/register an immutable economic policy with real audit references.
   Select its exact key/version/checksum in schema 4 GameConfig; publish via validated service/admin.
3. Create a DRAFT Season using the approved published config and matching dates. Activate via the
   admin activation form, with `arena.bind_economic_policy` and reviewed source-coverage evidence.
   This binds policy and initializes Epochs; it does not authorize weekly workers.
4. Verify compatible source producers and economic workers, deployed code identity, PostgreSQL direct
   connection or session pooling (transaction-mode PgBouncer is incompatible with worker session locks),
   queue routing, retention horizons, task limits and alert delivery. Capture evidence externally.
5. After explicit owner authority, an authorized operator can run:

   ```sh
   python manage.py control_dlx_automation SEASON_CODE --actor OPERATOR --enable \
     --reason 'approved activation' --evidence REVIEW_TICKET
   ```

   The operator needs `arena.control_economic_automation`. This operation validates every bound Epoch
   and config/policy identity and appends authority. The scheduler never creates approval or binding.
6. Ordinary healthy execution needs no per-week calculation/payment. Beat dispatches one Season job.
   It finalizes bounded Creator/Clan windows and Clan Mission signals using existing services, then
   Prepare/Qualification → Readiness → Freeze → chronological Settlement → category Reconciliation →
   Growth is evaluated inside Freeze before commitments are sealed. It preserves the configured gate and catches up earlier Epochs in order.
   Weekly jobs never claim Mission/Typing wallets. Season Close retains audited vested-XP settlement.
7. Pause safely:

   ```sh
   python manage.py control_dlx_automation SEASON_CODE --actor OPERATOR --pause \
     --reason 'operational pause' --evidence INCIDENT_TICKET
   ```

   Pause waits for any already-running stage transaction, then prevents the next stage. It does not
   reverse confirmed history. Re-enable requires another authorized control event. Source ingestion
   remains subject to existing policy/barriers; pausing economics does not delete queued work.

## Recovery, observability and limits

Each attempt records stage, control authority, status, failure category and reconciliation
results. Structured `dlx_automation_stage_result` logs include Season/Epoch/stage/status/error.
`operational_snapshot.dlx_automation` exposes status/error counts. The implemented
[economic alert scanner/outbox](ALERTS.md) detects deadlines, blocked/failed stages, Growth,
reconciliation, stale/repeated workers and failed/manual Close. Detection and delivery default off.
No real destination or external message is configured/sent in this PR. Owner-authorized routing,
credentials and an external application/Beat liveness monitor are activation prerequisites.

A PostgreSQL session advisory lock prevents overlapping Season workers. A killed process releases
it; the next owner marks abandoned RUNNING attempts INTERRUPTED and resumes idempotent stages.
Source and allocation barriers remain transaction-scoped. Freeze revalidates the source manifest
inside its exclusive barrier. A transient database/connection failure retries with backoff; scanner
re-dispatch recovers broker loss/restart. Unexpected failures stay observable, without force-ready.
Unknown corrections, reviews, source dispositions and disqualifications still require their existing
reviewed services. Final correction rounds remain separate close controls.

Runtime controls: `DLX_AUTOMATION_SCAN_SECONDS` (60), `DLX_AUTOMATION_SOURCE_BATCH_SIZE` (100),
`DLX_AUTOMATION_SOFT_TIME_LIMIT` (3600), `DLX_AUTOMATION_TIME_LIMIT` (3660). They are operational
bounds, not gameplay entitlements. Only one Beat schedule dispatches the lifecycle; old task names
remain compatible dispatch wrappers. There is no per-Season activation seed or default-on switch.

Large Prepare/Freeze transactions retain the exclusive Season writer barrier. Measured normalized
capacity does not prove acceptable gameplay latency under those barriers. Validate contention and
size worker resources before any large-Season authorization; see the capacity limitations.

## New Clan Missions during an active Season

Use a published config whose ID is exactly compatible with the existing ClanSeason. Create a NEW
Individual or Group definition/version with its requirements, target, schedule and supported band
(SMALL/MEDIUM/LARGE/GROUP), then activate it through the existing audited Clan Mission workflow.
The selected band resolves reward amounts from that definition's pinned config, not service constants.
Do not modify already activated/completed definitions or migrate existing bands to a new config.
Canonical progress signals and settlement remain automatic, transactional and exactly once.
