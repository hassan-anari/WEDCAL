# B4 backend handoff

Backend Draft [WEDCAL #29](https://github.com/hassan-anari/WEDCAL/pull/29), branch
`ai/b4-backend-integration`; consumer Draft [FROCAL #5](https://github.com/hassan-anari/FROCAL/pull/5).
This publication contains runtime implementation, not just the initial inventory. The exact
implementation commit is recorded in the PR coordination comment; `2d2a856` is documentation
only and must not be used as the implementation pin. Final CI and paired acceptance are pending.

## Source pins and integration

- Merged WEDCAL main: `1fa937af4628a10cec384b711210755ff28a6a8a`.
- FROCAL main inventory: `ae91624f23a48d8a9973154520284706a9df1c21`.
- Reviewed FROCAL #5 requirements/preservation head: `fe7e93c56303d4b02125a974fa29efde9d59031e`.
- Preserve the historical B3 pin; add a separate exact B4 implementation/fixture pin. Consume
  [actual OpenAPI](fixtures/b4/openapi.json), [contract](B4_BACKEND_CONTRACT.md) and HTTP fixtures.
- Existing authenticated Profile/Arena/Notifications/Daily/Missions/Typing/Referral/Network
  paths remain. Added fields are compatible; new history/read-all and optional Season queries
  are documented. Do not introduce UI controls from backend capabilities.
- Typing score preserves the existing display formula using authoritative submitted metrics;
  it is separate from wallet credit and canonical XP. Use nullable values honestly.
- Archive/DLX visible presentation remains an owner decision. Backend returns actual archives
  and off-chain allocations, with no claim, trade, transfer, on-chain or price functionality.
- Canonical progression owns balances. Reward reveals require a newly created confirmed ledger
  event and event-ID deduplication; zero/duplicate claims cannot fabricate a reveal.
- Optional photo URLs fall back to initials. Notification navigation uses `deep_link.path` only.
- Existing intentional Add Bot action may send `authorize_group_messages:true` only when its
  copy conveys that authorization. False/default never grants consent; no new Mini App toggle.

## Endpoint readiness

Implemented: Profile overview/achievements/collection/review/customization; Notifications
feed/read/read-all with safe links; player/clan rankings with pagination and archived standings;
Arena state and confirmed DLX; submitted Typing history; confirmed Daily/Mission/Typing reward
receipts; optional private invitee/group images; automatic bounded group re-verification.
All schemas and typed errors are exported from runtime routers. Mission publication checks and
runtime quarantine protect Bootstrap while preserving versioned verified claims.

Not activated: public Welcome, group image storage, wallet/account or Season 2 Warfront.
No missing endpoint is replaced with demo data. Frontend must test authenticated, empty,
error/retry, pagination, archive and cross-player cases against the final exact backend SHA.

## Migrations and operation

New migrations (no economic backfill):

- `network.0006`: intentional registration authorization and nullable bound intent.
- `notifications.0004`: nullable ingress timestamp for delivery latency.
- `notifications.0005`: Welcome version/singleton tables; no active seed template.
- `notifications.0006`: PostgreSQL immutable Welcome history trigger.
- `notifications.0007`: concurrent feed/read-state indexes, non-atomic migration.
- `players.0006`: optional private group image cache table, no historical download/enrollment.
- `typing_game.0002`: concurrent partial submitted-history index, non-atomic migration.

Keep existing recovery scanners. New durable rows dispatch through Celery after the outermost
commit; an unavailable broker leaves recoverable pending rows. Optional
`NOTIFICATION_INTERACTIVE_QUEUE` requires an actual consuming worker; empty uses existing queue.
Measure `telegram_interactive_delivery` queue/end-to-end latency with worker capacity and rate
budgets intact. No synchronous webhook Bot API calls or messages inside business transactions.

Public Welcome requires BOTH `B3_BOT_WELCOME_ENABLED` and `B4_PUBLIC_WELCOME_ENABLED`; the new
flag defaults false. Creating/previewing/activating/rolling back a template sends nothing.
Test-send is confirmed, audited and restricted to the requesting authorized operator's own chat.
No real test-send was run. Do not enable public Welcome without explicit owner authorization.

`GROUP_IMAGE_CACHE_ENABLED` defaults false. Configure a private Backblaze/S3 bucket and dedicated
`GROUP_IMAGE_S3_ACCESS_KEY_ID` / `GROUP_IMAGE_S3_SECRET_ACCESS_KEY` scoped only to `group-images/`.
Never broaden or reuse avatar credentials silently. Configure private bucket lifecycle/orphan
cleanup and existing bounded refresh/purge workers before enabling. Delivery URLs are opaque
expiring capabilities; do not log or publish them. No Telegram credentials/direct file URLs are
returned. See `.env.example` for exact bounds and configuration names.

Apply migrations before new workers/API; run concurrent indexes outside an encompassing atomic
transaction, check failed/invalid indexes before retry. Table additions/nullable fields leave
old code compatible. Rollback runtime first, leave additive tables/columns/history intact;
never drop economic ledgers. Disable image/public Welcome flags and drain newly routed queues
before reverting workers. Migration SQL and final verification evidence will accompany review.

## Verification status at implementation publication

Isolated PostgreSQL/Redis/RabbitMQ regression run: 830 passed, one opt-in benchmark skipped;
subsequent contract additions are undergoing final rerun. Real prefork Celery/webhook test uses
an isolated queue/test database and replaces only the HTTP transport with a synthetic recorder.
It contacts no Telegram users. Measured reply p95 1.34 s, webhook ACK p95 34 ms and callback ACK
87 ms; duplicate/unauthorized sends remain zero. Final CI, benchmark, migration and security
scan evidence will be recorded before claiming completion. These are isolated healthy-system
measurements, not a production latency guarantee.

No merge, deployment, production data modification or public message. Keep this PR Draft and
stop for independent review after required checks and paired contract verification.
