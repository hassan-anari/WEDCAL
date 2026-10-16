# B4 backend handoff

Backend Draft [WEDCAL #29](https://github.com/hassan-anari/WEDCAL/pull/29), branch
`ai/b4-backend-integration`; consumer Draft [FROCAL #5](https://github.com/hassan-anari/FROCAL/pull/5).
This publication contains runtime implementation, not just the initial inventory. Current runtime,
schema and fixture implementation HEAD: `ec6af7053dfc7e8755998418b826c59ca91191ea`.
A later documentation/evidence-only PR HEAD may contain this same implementation; `2d2a856` is documentation
only and must not be used as the implementation pin. Final CI and consumer acceptance are tracked below and in PR comments.

## Source pins and integration

- Merged WEDCAL main: `1fa937af4628a10cec384b711210755ff28a6a8a`.
- FROCAL main inventory: `ae91624f23a48d8a9973154520284706a9df1c21`.
- Reviewed FROCAL #5 requirements/preservation head: `fe7e93c56303d4b02125a974fa29efde9d59031e`.
- Preserve the historical B3 pin; add a separate exact B4 implementation/fixture pin. Consume
  [actual OpenAPI](fixtures/b4/openapi.json), [contract](B4_BACKEND_CONTRACT.md) and HTTP fixtures.
- Existing authenticated Profile/Arena/Notifications/Daily/Missions/Typing/Referral/Network
  paths remain. Added fields are compatible; new history/read-all and optional Season queries
  are documented. Do not introduce UI controls from backend capabilities.
- Typing TODAY uses `/typing/history?period=current`, with server `period_key`, `period_start`,
  `period_end` and per-run `reset_period`. Typing's reset timezone is independent of Daily.
  Default `period=all` keeps historical paging. A stale current-period cursor gets
  `TYPING_PERIOD_CHANGED`/409: clear accumulated TODAY pages and refetch page one; never infer
  client calendar dates. Exact-reset, cross-boundary submit, paging, empty and differing-timezone
  HTTP fixtures are in `typing-periods.json`.
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

## Final implementation verification

Runtime/source/fixture pin: `ec6af7053dfc7e8755998418b826c59ca91191ea`.
All hosted checks are green on that implementation:

| Gate | Evidence |
| --- | --- |
| Contract | [run 38869293906](https://github.com/hassan-anari/WEDCAL/actions/runs/38869293906) |
| Foundation quality/test/Docker | [run 38869293917](https://github.com/hassan-anari/WEDCAL/actions/runs/38869293917): 845 passed; benchmark separately 1 passed; PostgreSQL migrate/no drift, Ruff/format/mypy, Django and three OpenAPI drift checks, Docker runtime/Admin/static smoke |
| Security | [run 38869293916](https://github.com/hassan-anari/WEDCAL/actions/runs/38869293916): dependency audit has no known vulnerabilities; container 0 CRITICAL/0 fixable HIGH; 44 unfixed HIGH package instances across eight existing reviewed IDs |
| Local full suite | 845 passed in 280.78 s; one opt-in benchmark omitted from general suite and separately passed |
| Committed fixtures | 19 new response bodies validate against actual runtime schemas; deterministic reset-period HTTP fixture included |
| Paired compatibility | Unchanged FROCAL `fe7e93c56303d4b02125a974fa29efde9d59031e` tests against exact backend runtime above: 29 API/controller + 13 browser passed |

The general-suite benchmark skip is intentional; the required CI benchmark step actually runs
and passes. No required GitHub job is skipped. PostgreSQL 17.11/Redis 8/RabbitMQ 4.3 are isolated
synthetic services. No production connection, data reset, real Telegram contact or deployment.

[Machine-readable verification](evidence/b4-verification.json),
[local latency samples](evidence/b4-interactive-latency.json),
[PostgreSQL benchmark](evidence/b4-b2-benchmark.json),
[review-only migration SQL](evidence/b4-migrations.sql), and
[security review/dispositions](B4_SECURITY_REVIEW.md) are committed. Hosted Foundation retains
[b4 latency artifact](https://github.com/hassan-anari/WEDCAL/actions/runs/38869293917/artifacts/12032254416)
and [benchmark artifact](https://github.com/hassan-anari/WEDCAL/actions/runs/38869293917/artifacts/12032856456).
Hosted Release Scans retains raw JSON/SARIF/policy. No ignore or gate suppression was introduced.

The latency proof runs real prefork Celery, webhook ingestion, PostgreSQL transactions, RabbitMQ
and Redis budgets; only HTTP transport response bytes are synthetic. Twelve healthy interactions:
reply p95 1.60 s, webhook ACK p95 86 ms, callback ACK 413 ms; zero duplicate/unauthorized sends.
No real users were contacted. This is not WAN, Telegram API, production-capacity or physical-device
latency certification. Keep recovery scanners and instrument actual queues before activation.

## Paired acceptance and independent review

Consumer interim head `815c00457169e61cf6f59fe7686e0a539d7eec98` reports 8 actual B4 API/controller
and 15 browser checks against previous `9a43048` in
[this coordination comment](https://github.com/hassan-anari/WEDCAL/pull/29#issuecomment-6210605276).
The corrected Typing period implementation above was shared on both PRs in
[backend comment](https://github.com/hassan-anari/WEDCAL/pull/29#issuecomment-6210682818) and
[frontend comment](https://github.com/hassan-anari/FROCAL/pull/5#issuecomment-6210684584).
The consumer must pin that successor separately from B3 and confirm its final B4 binding/CI;
backend compatibility tests do not falsely stand in for that acceptance. Historical B3 pins were
not overwritten. The final PR documentation-only HEAD may differ from the runtime pin without
changing runtime/schema/fixture content; exact PR HEAD is posted in the review comment.

Archive selection and visible DLX UI are explicitly deferred owner design decisions; existing
current-season rankings are complete and no new control/card/panel is required. First Verified
Activity has no approved secondary active-day evidence. Do not fabricate it.

No merge, deployment, production data modification, public message or real test-send. Keep Draft;
stop for independent review. Public Welcome stays disabled. B5 is not authorized.
