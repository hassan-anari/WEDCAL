# B4 backend handoff

Backend Draft [WEDCAL #29](https://github.com/hassan-anari/WEDCAL/pull/29), branch
`ai/b4-backend-integration`; consumer Draft [FROCAL #5](https://github.com/hassan-anari/FROCAL/pull/5).
This publication contains runtime implementation, not just the initial inventory. Owner-policy runtime implementation SHA: `3fd6d87a1d738b8ba78d98788c76b81cb1dc7715`.
Final review HEAD (additional test/fixture/evidence corrections) and exact consumer pin are posted
on PR #29; production application/migration code remains identical to this implementation.
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
- Add Bot remains the existing Creator registration flow. No broadcast opt-in/checkbox/flag is
  required. Optional legacy intent/preference API fields remain compatible but never gate broadcasts.

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

B4 migrations (no economic backfill):

- `network.0006`: intentional registration authorization and nullable bound intent.
- `notifications.0004`: nullable ingress timestamp for delivery latency.
- `notifications.0005`: Welcome version/singleton tables; no active seed template.
- `notifications.0006`: PostgreSQL immutable Welcome history trigger.
- `notifications.0007`: concurrent feed/read-state indexes, non-atomic migration.
- `players.0006`: optional private group image cache table, no historical download/enrollment.
- `typing_game.0002`: concurrent partial submitted-history index, non-atomic migration.
- `notifications.0008`: direct nullable group FK, campaign policy version, chat uniqueness and target/
  immutable-policy guards. Existing campaigns are version 1; new confirmed campaigns use version 2.
  Legacy subscription rows/targets are retained, not deleted or rewritten.

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
transaction, check failed/invalid indexes before retry. For the broadcast-policy migration, pause broadcast workers/materialization and inspect active
campaigns and duplicate (campaign, chat) rows before applying. Existing DRAFT campaigns can be
confirmed under version 2. Version-1 MATERIALIZING campaigns must be cancelled and recreated using
the existing confirmation flow; never reinterpret their old subscription cursor. Legacy queued
targets remain fixed and revalidate current bot authority. The new unique constraint validates
the recipient table under a schema lock: assess table size/lock budget in the rollout window.
New application instances default to version 2; database default 1 safely labels old-writer inserts.
Before rolling back workers, stop/cancel/drain new direct-group deliveries with new code and resolve
in-flight uncertainty. Old workers do not understand direct group targets. Retain additive schema
and history rather than unapplying 0008 over new recipients;
never drop economic ledgers. Disable image/public Welcome flags and drain newly routed queues
before reverting workers. Migration SQL and final verification evidence will accompany review.

## Previous implementation verification (before owner policy correction)

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


## Owner policy correction handoff — 2026-10-16

The direct owner instruction supersedes all previous consent/revocation eligibility prose:
all privately started contacts and all known bot-authorized admin groups/supergroups are operator
broadcast candidates, including pre-B4 and non-Creator groups. Legacy preference rows and creator
identity are not eligibility gates. Actual sending revalidates configured bot identity/admin/send
permission and committed membership loss. Telegram forbidden/chat-not-found skips only that target.
Unknown groups cannot be enumerated; existing membership records/updates are the discovery source.
No public Welcome activation, actual campaign, production migration or real test-send occurred.

FROCAL #5 must replace only its separate B4 backend source/fixture pin and paired CI checkout with
the new runtime SHA posted on #29. Preserve historical B2/B3 pins. Synchronize the generated B4
OpenAPI/fixtures; legacy consent fields keep their wire shape but are documented as deprecated
metadata. Keep Add Bot unchanged: no checkbox, opt-in request or new UI control. Rerun original
B2/B3 and B4 API/controller/browser checks on isolated PostgreSQL/Redis with synthetic transports.
The successor changes operator audience semantics, not Profile/Typing/reward/canonical values.

Focused tests: 137 pre-existing/revised regressions passed; 22 owner-policy/migration tests passed,
including no subscription, historical/unrelated groups, recorded/fresh permission loss, blocked
user/group, real mixed send budgets/retry/idempotency, PostgreSQL concurrency/uniqueness and actual
0007→0008 migration retention. Full successor CI and exact SHA evidence follow in the PR comment.
Previous 845-test/CI evidence above proves only the prior runtime and must not be presented as
verification of this correction. Stop after the successor checks are green for independent review.


### Successor verification and exact frontend update

Production code/migration implementation: `3fd6d87a1d738b8ba78d98788c76b81cb1dc7715`. Local full regression: **868 passed**
in 248.33 s; opt-in benchmark is separately required in hosted CI. An additional legacy queued-target
regression plus refreshed persisted-campaign fixture passed (2 focused tests). Final CI includes
**869 tests** with that added regression; use the final review-HEAD CI links in the PR comment as
completion evidence rather than the earlier 845-test or pre-correction pins.

Successor contract run [38913664223](https://github.com/hassan-anari/WEDCAL/actions/runs/38913664223)
and [Release Scans 38913663381](https://github.com/hassan-anari/WEDCAL/actions/runs/38913663381)
passed; Foundation [38913663997](https://github.com/hassan-anari/WEDCAL/actions/runs/38913663997)
contains quality, full PostgreSQL/test/benchmark and Docker runtime proofs. Final review-HEAD runs
are recorded in the PR after this evidence commit. Local ruff/format/mypy (419 files), Django,
OpenAPI B3/B4 drift and migration/no-drift passed; migration SQL is
[b4-owner-policy-migration.sql](evidence/b4-owner-policy-migration.sql).

Security policy remains unchanged: no known dependency vulnerabilities, zero CRITICAL/fixable
HIGH, the same eight reviewed unfixed HIGH IDs across 44 package instances. See
[b4-owner-policy-trivy-policy.json](evidence/b4-owner-policy-trivy-policy.json). Shared transport
classification now preserves definitive Telegram forbidden/chat-not-found as terminal per-target
skips; other API/error schemas remain compatible. No new dependencies, flags or public Welcome
activation. Local interactive reply p95 remained 1.50 s with zero duplicate/unauthorized/real sends.

FROCAL #5 instructions:

1. Use the **final review HEAD posted on #29** as the exact B4 source/fixture/paired-CI checkout
   pin (it contains runtime `3fd6d87a1d738b8ba78d98788c76b81cb1dc7715` plus corrected persisted campaign metrics and tests).
2. Preserve B2 and historical B3 pins unchanged. Refresh only B4 pinned schema/fixture hashes,
   including `broadcast-policy.json`; its campaign metrics come from a refreshed database row
   and show COMPLETED with real per-audience counts.
3. Keep all existing UI/Add Bot requests unchanged. Do not send an opt-in flag or add consent UI.
   The optional legacy flag remains accepted solely for compatibility metadata.
4. Run original B2/B3 and B4 API/controller/browser regressions against that exact checkout with
   isolated PostgreSQL/Redis and mocked Telegram transports. Report exact consumer SHA and evidence
   in both paired PRs. Backend CI does not falsely claim final consumer acceptance.

No merge/deployment/production write/message; stop for independent review after final HEAD green.
