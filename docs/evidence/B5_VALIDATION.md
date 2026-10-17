# B5 initial implementation local validation

Scope: the initial B5 runtime `c38a7a65ba9bacce53a04bec6c10dfbeb71ed008` and fixtures in Draft PR #30. The later independent-review correction, new runtime pin and final-head verification are recorded in [B5_SINGLE_APPLICATION.md](B5_SINGLE_APPLICATION.md) and the final PR review comment. Synthetic Telegram transports, PostgreSQL 17.11, Redis 8 and isolated RabbitMQ; no live Telegram, storage verification, campaign or public test send. Backend base `6aaa362daecec86107babd95224398e7e1ac7e75` was fetched and confirmed unchanged before this run.

| Check | Result |
| --- | --- |
| Full `pytest -q --maxfail=1` | **980 passed, 1 skipped**, 350.83 seconds |
| Separate `B2_BENCHMARK=1 pytest apps/base_tap/tests/test_benchmark.py -q` | **1 passed**, 50.59 seconds |
| Ruff check / format | Passed |
| mypy | Passed; 435 source files |
| Django system check | No issues |
| Migration drift | No changes detected |
| Repository contract | Passed |
| B2 / B3 / B4 / B5 generated OpenAPI checks | Passed |
| Latest-main ancestry and `git diff --check` | Passed |

The final full run includes removal of resolved PH-045 from `state.owner_decisions_pending` and five additional same-second/precise evidence regressions. Whole-second Telegram loss evidence cannot be hidden by a same-second verification; precisely timed older API results remain stale. Positive restoration is fenced against committed transitions during remote calls. Rapid loss/restoration/relapse preserves economic pause and canonical source-time accounting. HTTP fixtures were exported in the corresponding isolated integration runs.

The sole full-suite skip is the opt-in B2 benchmark, executed successfully in the separate row. One existing Ninja tuple-response deprecation warning remains; no test failure is suppressed. Test runs were serialized against the isolated database.

`b5-b2-benchmark.json` contains the separate benchmark measurements (64 synthetic Players, 16 workers, 512 accepted batches, 12,800 taps). `b5-b4-interactive-latency.json` contains the compatibility latency samples from synthetic Telegram HTTP. These are local evidence, not production/device latency claims.

The full suite includes PostgreSQL transaction/concurrency tests for existing Clan economics, capacity/credential state machines and governance, plus new source-time mission adapters and authority-loss races. It also includes actual Django Admin multiline CRLF/LF/4096-character submissions, HTML negatives, banner security/empty/external destinations, strict avatar path/MIME/size regressions, announcement permission/snapshot/idempotency/batch/retry/cancel races, native-media and album delivery/preview/partial/ambiguous/cancellation/rate/auth/destination failure regressions, and B2/B3/B4 compatibility.

Rendered SQL:

- `B5_CLAN_MIGRATION.sql`: durable mission signals.
- `B5_AUTHORITY_RECOVERY.sql`: independent recovery causes and evidence/redrive timestamps.
- `B5_NOTIFICATIONS_MIGRATION.sql`: source/announcement structures and delivery fields.
- `B5_SNAPSHOT_GUARDS.sql`: immutable announcements/templates/source references and monotonic progress.
- `B5_FROZEN_SOURCE.sql`: first complete preview source binding and immutable native preview result.

Final full runtime SHA and hosted CI links are recorded in Draft PR #30 and paired FROCAL Draft #6 handoff. Required hosted checks cover contract, quality/types/schema, full PostgreSQL regression and benchmark, container/Admin/service smoke, dependency security and image scans. Earlier green runs do not substitute for final runtime checks. This document does not claim physical-device or frontend paired acceptance.
