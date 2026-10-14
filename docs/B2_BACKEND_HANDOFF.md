# B2 backend handoff — independent review required

[Backend Draft PR #27](https://github.com/hassan-anari/WEDCAL/pull/27), branch `ai/b2-backend`. C-01/P-02 and backend P-03 delivered as one B2 batch. Paired frontend [Draft PR #3](https://github.com/hassan-anari/FROCAL/pull/3), inspected at `f538f1cc42dc34d7ba21725432915244f34b8005`, is waiting for this committed wire contract; binding/E2E is still its pending work. Repository names/remotes/URLs preserved. No merge, deployment, deployed DB mutation or public economic/load test performed. [Wire contract](B2_TAP_V2_CONTRACT.md).

The [independent-review compatibility amendment](B2_CONTRACT_COMPATIBILITY_FOLLOWUP.md) adds pinned cooldown duration, immutable receipt identity and coordinated avatar budgeting in the same PR. Frontend must pin the follow-up SHA recorded in the PR description.

## Migration and settlement inventory

- `base_tap.0002_tapburststate_taplease`: additive per-player burst and lease metadata.
- `rewards.0006_receipt_cursor_index`: concurrent `(player,id DESC)` index prevents receipt-cursor snapshots scanning unlimited player history. Nonatomic concurrent DDL; if interrupted with an invalid index, migration refuses success: owner must drop that invalid index concurrently and rerun. No ledger rewrite.
- `players.0005_playeravatar`: metadata-only cache; no database image bytes.
- No automatic data migration, ledger rewrite or published config edit. Static infrastructure fix itself needs no DB migration.
- Atomic v2: shared Season barrier → shared epoch row → Player → domain state; consume allowance, append immutable ledger receipt/source fact, update progression/Season through canonical primitive, persist ranking projection. No referral outbox or direct DLX.
- Legacy finalizer now settles ledger and drains accumulator in one transaction, preserving `base-tap-day:<id>`. Old PENDING/PROCESSING/RETRY and the former ledger-committed/drain-not-committed crash window reconcile exactly once. Already accepted legitimate XP survives later moderation. No projection reset/rebuild; historical 150-point entry remains untouched.

## Historical reconciliation and cutover

All deployed operations require owner approval. Test a restored backup first; this task's evidence is synthetic, not a deployed-user inventory.

1. Back up and verify restore; save config IDs/checksums, Season, old projections and deployment SHA. Preserve owner 150 Lifetime Points; never run destructive resets to make comparisons pass.
2. `python manage.py reconcile_legacy_taps --limit 500 --after-id 0` is read-only. Archive JSON with observed time, accumulator/player/Season/config IDs, accepted/pending/settled XP, state, original ledger ID, retained receipts, durable fact XP and consistency. Page with `next_after_id` until empty. Independently compare ledger and projections; discrepancies require review, never silent repair.
3. Pause legacy ingress while v2 issuance remains false. Stop **old-version** API/worker/beat processes so none can increment/finalize with old code. Upgrade all workers before drain. Retained legacy receipts remain replayable.
4. After operational approval, `reconcile_legacy_taps --apply --limit 500 --after-id …`. Refuses apply if ingress is enabled or facts/ledger fail reconciliation. Bounded/restartable/exactly once; archive before/after stdout. Never delete historical ledger, facts, accumulators or published configs.
5. Repeat dry-run: pending zero, settled ledger/fact agreement, old projections plus exactly drained XP, no unresolved processing/retry errors. Existing close barrier/discrepancies are blockers; do not bypass them.
6. Keep legacy recovery/purge tasks. Receipt expiry never erases durable obligation/facts; recovery depends on accumulators and original ledger keys, not receipt survival.

## Rollout/rollback

1. Independent draft/CI review and explicit owner approval before merge; main auto-deploys. Paired frontend E2E/device review is separate activation prerequisite.
2. Additive code/schema with old config: v2 issuance=false, legacy=true, avatar=false. Verify manifest/Admin/readiness; no startup collectstatic.
3. Publish new reviewed schema-3 config at safe effective time (v1→v2 midnight UTC). Pause/drain legacy in approved cutover window; frontend must understand paused legacy responses.
4. Enable v2 only with legacy paused and pending obligations reconciled. Watch source mint, replay/gap/takeover/429, transaction/player-lock latency, facts, table/WAL growth.
5. Rollback disables **issuance only**. Keep B2 redemption code/config/migrations until authorized leases expire/drain. Do not revert to pre-B2 binaries or reopen the daily writer while valid leases exist. Committed receipts stay replayable indefinitely. A later approved v1 return needs a newly published config, fully expired/drained v2 leases and explicit frontend rollback. Never undo committed points.

## Avatar status and required configuration

Disabled/fallback default. Repo previously had container filesystem storage only; no shared storage credentials found. Testable private S3 adapter and cache are implemented; **VPN-off delivery remains unverified operationally**. No live Telegram/storage request or filtered-network claim.

Set `AVATAR_CACHE_ENABLED=true`, private `AVATAR_S3_BUCKET`, region, optional HTTPS S3 endpoint, and `AVATAR_PUBLIC_API_ORIGIN=https://api.dalexio.com`. Supply SDK AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (optional session token) or workload role through secret management. Limit permissions to GetObject/PutObject/DeleteObject on `avatars/*`; block public access. Adapter requires SSE AES256/TLS. Configure finite seven-day bucket lifecycle and incomplete upload cleanup for orphan/failed-delete backstop. Enable workers/Beat; monitor due/expired/error backlog. All Bot API callers share existing Redis global/method counters and a bot-wide 429 pause. Avatar work fails closed if that budget is unavailable. Set `AVATAR_CACHE_MAX_CONCURRENT_REFRESHES` consistently on every worker (default 2, allowed 1–16); PostgreSQL session advisory slots bound simultaneous refreshes across workers. This requires direct/session-pooled PostgreSQL connections; transaction-pooling proxies are unsupported for these session locks. Rate-limit/transient retries persist in `refresh_after` (server retry delay or local minimum plus jitter); workers do not sleep. Photo changes cannot bypass pending error backoff. File downloads also conservatively debit the shared global budget. Initial TTL 7d, 2MiB, 16M pixels, JPEG re-encoding/512px removes metadata. Refresh/photo removal rotates delivery UUID and deletes replaced bytes; purge retries storage failures.

Signed Telegram photo metadata only triggers refresh; **it is never fetched**. Worker gets verified player's photo through fixed Bot API methods, validates photo paths, rejects redirects/private resolved source IPs and bounds MIME/bytes/time/pixels. No arbitrary URL endpoint. Private Telegram URLs/tokens never reach browser/logs. Bootstrap performs no external/broker I/O. Existing fallbacks remain. Delivery uses controlled opaque UUID bearer URL through API: possession permits image read until expiry, with no Telegram ID/storage key/token disclosed. Account records/URLs are separate; frontend preserves B1 cache isolation.

Before claiming VPN-independent operation: test actual returned delivery origin on owner's filtered network, storage outage, account switch, image refresh/removal and deletion through deployed storage.

## Security and rollout gates

HTTP cannot prove a real finger. No auto-ban/hidden daily cap. Maximum theoretical 50 XP/10s is 432,000 XP/day before human/transport overhead: owner must review inflation and existing provisional Level pacing. No other economy/threshold changed.

Local benchmarks do not establish 50K/1M/public capacity. Accepted ledger/fact rows grow per batch, up to 50/burst with valid fragmentation; partition/archive/storage sizing and SLOs require owner-reviewed scale tests. PH-037 branch protection and PH-043 mission evidence remain unrelated launch gates. Paired frontend/device E2E and configured avatar origin remain activation prerequisites.

STOP after unmerged draft PR/report; no next batch authorized.

## Validation and changed paths

[Local tests, PostgreSQL benchmark, lock/write growth and Docker evidence](evidence/B2_LOCAL_VALIDATION.md); [raw benchmark](evidence/b2-benchmark.json); [synthetic legacy drain](evidence/legacy-drain.json). CI/PR links and final SHA are recorded in the PR description; they remain separate from local evidence. Changed files are fully listed in the PR diff: Base Tap models/services/schemas/API/tasks/tests/command; game config parser/sample/contracts; Arena source snapshot reconciliation; reward primitive/cursor index; Player avatar metadata/adapter/tasks/tests and Bootstrap/Profile URL binding; settings/Docker/CI/deploy smoke; wire fixtures and handoff/evidence.
