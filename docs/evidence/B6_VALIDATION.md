# B6 validation evidence

## Independent source-time lifecycle follow-up

Reviewed head: `28a6b8805db5f9a8c70b78c5eadf5c6276dbccd1`. Current runtime/source/database-schema revision: **`e99aa45474f7a9f372738a95eb96c2716b06b1d4`**. Main re-fetched and unchanged at `b4c72f292ab0ea967d62af7b8e5fdcab1f27e03f`, already an ancestor. The full [lifecycle review](../B6_MISSION_LIFECYCLE_REVIEW.md) documents the confirmed defect, owner-approved prospective observation/uncertain-history holds, transactional safety and rollback requirements.

| Check | Follow-up evidence |
| --- | --- |
| Full local PostgreSQL suite | **1095 passed, 1 opt-in benchmark skipped**, 343.70 seconds; one pre-existing Ninja tuple-response deprecation warning |
| Final lifecycle PostgreSQL rerun | **17 passed**, 27.82 seconds |
| Existing mission/Admin/concurrency/pipeline regressions | **54 passed**, 39.12 seconds |
| Separate B2 benchmark | **1 passed**, 35.91 seconds |
| Ruff / formatting / mypy | Passed; mypy 451 source files |
| Django system check | No issues |
| Migration / drift / SQL | Migration 0019 applied only locally; real reverse/forward PostgreSQL cutover tested; no model drift; full rendered SQL committed |
| Contract / B2–B6 OpenAPI | Passed; HTTP schema unchanged, SHA-256 `6dabd02986eebe01c0a605670d2d99becb8cda2b012e1088c9a03f0708d96993` |
| Final-code container / Admin static smoke | Local image built; Admin login and hashed static asset HTTP 200, non-root UID 999, DEBUG false |
| Source/economic scope / whitespace | No GameConfig, Clan economy, reward primitive or Clan HTTP API implementation changes; diff check passed |

The 17 added cases cover Group/Individual delayed settlement after deactivation, pre-activation and inactive-gap rejection, multiple cycles, exact schedule/lifecycle boundaries, participant eligibility, replay/worker retry, transaction rollback, stale full-cycle previews, database guards, both real PostgreSQL Admin/worker lock orderings, Season/recovery/finalization, published config identity and actual migration preservation of historical progress/settlements/processed signals. Known ineligible signals terminate without progress; uncertain history is held without attempts/rewards and remains a Season-drain obligation.

Final exact artifact head and complete hosted Contract/Foundation/Release Scans links, conclusions and counts are published together in [Draft PR #31](https://github.com/hassan-anari/WEDCAL/pull/31) and the FROCAL handoff. No earlier green result substitutes for final-head checks. New frontend acknowledgement/repin/paired evidence is required in [FROCAL #8](https://github.com/hassan-anari/FROCAL/issues/8) / [Draft #9](https://github.com/hassan-anari/FROCAL/pull/9); prior paired acceptance remains evidence for the old backend pin only. No production migration, historical resolution, reward backfill, live bot send, merge or deployment.

## Prior B6 evidence (superseded runtime)

All tests use isolated local/CI PostgreSQL, Redis, RabbitMQ and synthetic Telegram/storage transports. No production requests, test rewards, credits, broadcasts, migrations or backfills are part of this evidence.

| Check | Evidence |
| --- | --- |
| Locked dependencies | `uv sync --frozen` passed |
| Full local suite, complete accounting code | **1077 passed, 1 skipped**, 286.93 seconds; the skip is the separately executed opt-in benchmark |
| Separate B2 PostgreSQL benchmark | **1 passed**, 37.96 seconds |
| Expanded B6 suite after Admin stale-edit fence | **35 passed**, 37.48 seconds |
| Ruff / formatting / mypy | Passed; mypy 449 source files |
| Django system check | No issues |
| Migration execution / drift | Applied to isolated PostgreSQL; no changes detected |
| Repository contract and B2/B3/B4/B5/B6 OpenAPI drift | Passed |
| Final-code local container / Admin static smoke | Built successfully; Admin login and hashed static asset HTTP 200, non-root UID 999, DEBUG false |
| Current-main ancestry / diff whitespace | Passed against b4c72f292ab0ea967d62af7b8e5fdcab1f27e03f |

The 1077-test run began before the separate Admin stale-edit fence was imported; that focused successor is covered by the 35-test run and the current-runtime/full hosted suite. A final local full successor run is also executed; final exact counts and all hosted check states are posted in Draft #31. One existing Ninja tuple-response deprecation warning remains. No failed test is marked skipped or suppressed. Two existing test setups were updated to the explicitly approved behavior: schedule before activation, and a second distinct /start replies while duplicate redelivery does not.

Runtime/source pin: `13e7b1988181980b6ad80b69a8e9f28347e31802`. Its hosted [Contract 39033192356](https://github.com/hassan-anari/WEDCAL/actions/runs/39033192356), [Foundation 39033192378](https://github.com/hassan-anari/WEDCAL/actions/runs/39033192378) and [Release Scans 39033192307](https://github.com/hassan-anari/WEDCAL/actions/runs/39033192307) run the unchanged required workflows, plus the new B6 schema gate. Final documentation/fixture artifact-head check links and counts are recorded in the PR handoff; no earlier green run substitutes for required current-head checks.

Release Scans at the runtime pin passed dependency audit and container actionable policy. Trivy retained SARIF, raw JSON and policy: **44 HIGH finding instances, zero fixable HIGH/CRITICAL, eight unique no-fix IDs**, `passed: true`. IDs: CVE-2025-69720, CVE-2026-16742, CVE-2026-54369, CVE-2026-76642, CVE-2026-78408, CVE-2026-78409, CVE-2026-78410, CVE-2026-9538. These match the individual accepted runtime applicability dispositions in [PHASE11_CONTAINER_SECURITY.md](../PHASE11_CONTAINER_SECURITY.md); B6 adds no executable/privileged path exposing those tools. No blanket suppression, security policy change or dependency downgrade is introduced. Final-head scans are still required.

Rendered migration SQL is committed separately. B6 tests cover controlled Admin Group/Individual creation and automatic settlement, unsupported/unpublished/invalid configuration and permission/signed-preview negatives, database history guards, stale edit fencing, one Redis read per applicant/discovery page, canonical Daily streak resets, Arena rank/generation/readiness, private feed provenance/keyset limits/isolation, JOIN/REJOIN retrieval/retry/cancel/revoke cleanup, old credential delivery blocking, Network registration race exclusion and group fallback, incoming Telegram/window/horizon/mission pipeline, Treasury fractional carry/zero-net/concurrency/retry/version/rollback/history/moderation/Season boundaries, and distinct /start/template/delivery outcomes.

Frontend acknowledgement, pinned paired CI, staging/device WebView acceptance and realistic load measurements remain consumer/owner evidence. PH-037 and PH-043 remain release gates. All backend checks must be green on the final Draft head before independent-review handoff; no merge/deploy is authorized.
