# B3 backend validation evidence

Date: 2026-10-15. Draft: https://github.com/hassan-anari/WEDCAL/pull/28.
Branch `ai/b3-backend-integration`. Final exact-head SHA/check URLs are recorded in the PR so this
file does not require a self-referential commit SHA. No merge/deploy/activation/production mutation.

## Executed local checks

| Command / scope | Observed result |
|---|---|
| `B2_BENCHMARK=1 .venv/bin/pytest -q --tb=short` | **761 passed**, no skips, 208.97s, one existing Clan tuple-response deprecation warning; functional head `4fac1af91d374bdb1802c1e7dccf959ea2f8fafa` |
| Full notification/admin/broadcast suite after final private-routing/feed changes | **55 passed**, 45.20s |
| Final Bot Admin/welcome matrix, including expired sequence restart and `/start` during composition | **25 passed**, 29.04s |
| Final user/group audience and negative consent authorization matrix | **16 passed**, 22.07s |
| Avatar suite plus Admin upload check | **24 passed**, 12.50s; valid octet-stream JPEG, bad/HTML bytes and legacy formats included |
| Paired recovery/durable marker/latency suite | **9 passed**, 24.86s |
| `ruff check .`, `ruff format --check .`, `mypy .` | Passed; 396 Python source files type checked |
| `manage.py check` | No issues |
| `manage.py makemigrations --check --dry-run` | No changes detected |
| `scripts/validate_contract.py` | Passed |
| Both OpenAPI export `--check` commands | Passed |
| Secret-free production manifest collectstatic/Admin smoke | Login 200, hashed CSS 200, DEBUG false; [JSON](b3-admin-static-smoke.json) |
| `git diff --check` | Passed |

[Raw successful local results](B3_LOCAL_TEST_RESULTS.txt).
Targeted suites overlap the full suite; their counts must not be added as unique tests. Final
changes add two private-bot regressions and six group-consent negatives beyond the 761-test full
run, and are validated by the subsequent targeted runs and final exact-head GitHub test job.

An earlier pair of local pytest processes accidentally overlapped on the same synthetic database
and produced flush/deadlock errors. Those runs were stopped and discarded. The complete 761-test
run above was serialized; no production database was used or reset. No failed/interfered run is
presented as passing evidence.

## PostgreSQL concurrency and economics

Actual PostgreSQL (not SQLite or mocked transaction behavior). Existing Daily/Mission/Typing,
Referral/Creator, Clan/Season and B1/B2 regression coverage remains. Added paired tests verify:
A owns/B denied; B explicit takeover/A fenced; no automatic reclaim; intentional regain; closed
clients and new entry; stale release; parallel takeovers; reconnect/expiry with pending batches;
lost immutable receipt; cooldown; concurrent unrelated Daily/Mission settlement; unchanged
Lifetime/Season projections; preserved historical 150 points; fencing after lease cleanup.

[Migration SQL for review](B3_MIGRATION_SQL.sql) renders all four new migrations. They add tables,
nullable provenance/target fields, constraints/indexes and transport metadata; no economic history
or published GameConfig snapshot is rewritten. Recipient check/index validation may lock/scan a
large table: review actual production size and migration timing before any authorized rollout.

## Recovery measurements

20 samples per scenario, local sequential service transactions with PostgreSQL/Redis. No WAN,
physical Telegram device, public load or production mutation. [Raw JSON](../fixtures/b3/session-recovery-latency.json).

| Scenario | p50 ms | p95 ms | max ms |
|---|---:|---:|---:|
| Explicit state-read + switch | 43.747 | 58.439 | 103.550 |
| Release + intentional regain | 42.015 | 53.261 | 64.205 |
| New client after passive expiry | 26.625 | 34.936 | 36.042 |
| Immediate explicit recovery after both closed | 42.083 | 57.267 | 62.512 |
| Lost-response receipt replay | 18.917 | 26.401 | 34.101 |
| Rejected expired/fenced pending input | 14.343 | 22.479 | 35.427 |

Passive expiry remains 120 seconds and does not meet the UX target by itself. Explicit takeover
provides the fast path. Cooldown remains 10 seconds; tests advance the server clock for that wait,
so these DB measurements must not be reported as actual elapsed WAN/cooldown recovery. Physical
Android/iOS/Desktop click-to-recovery against 10–15s preferred / approximately 20s maximum remains
a paired FROCAL/owner acceptance gate. A disconnected client cannot receive instant remote
revocation knowledge; PostgreSQL authority is fenced before any successor lease response.

## B2 load regression

[Raw benchmark](b3-b2-regression-benchmark.json): 16 workers, 64 synthetic players, 512 accepted
microbatches plus one same-player duplicate-contention reward. 513 ledger receipts match 513
source facts; 16 duplicate requests produce one new reward and 15 original receipts. Local
37.781s wall time, 13.552 accepted batches/sec; batch p95 1209.044ms; same-player duplicate p95
735.713ms. These are measurements of this small cloud workspace, not a scale/capacity/SLO claim.
Virtual cooldown and cumulative table-stat limitations are preserved in the raw report.

## CI and independent review

At functional head `4fac1af91d374bdb1802c1e7dccf959ea2f8fafa`, all four applicable checks passed:
[quality](https://github.com/hassan-anari/WEDCAL/actions/runs/38745070044/job/116456468885),
[test + opt-in benchmark](https://github.com/hassan-anari/WEDCAL/actions/runs/38745070044/job/116456469209),
[docker-build/application/static smoke](https://github.com/hassan-anari/WEDCAL/actions/runs/38745070044/job/116456469495),
[contract-gate](https://github.com/hassan-anari/WEDCAL/actions/runs/38745070092/job/116456478926).
The final PR records checks for the subsequent final delivery commit; prior-head green does not
substitute for that gate. No dependency manifest/base-image changes were made in B3.

## Remaining dependencies and limits

- PH-043: approved Season brief content/completion evidence; endpoint explicitly unavailable,
  no invented verifier/reward. PH-037 branch protections remain the existing owner release gate.
- The owner confirms FROCAL Draft #4 now pins backend
  `88b26bea8e61f68adea4d2652b531f26cb5171d7`; the subsequent banner credential follow-up does not
  change frontend schemas/fixtures. Paired mobile/Desktop acceptance remains. No frontend edits here.
- No live welcome/broadcast/Backblaze write or real webhook delivery proof was performed. All
  outbound Telegram/storage transports in feature tests are mocked; the owner supplied the
  existing live avatar path PASS, and B3 only commits the narrow hotfix parity change.
- B4/B5/B6, production rollout and owner acceptance remain outside this PR's execution authority.

Stop after exact-head CI for independent review. Do not merge or deploy automatically.

## Independent-review follow-up: banner credential isolation

Optional `BANNER_S3_ACCESS_KEY_ID` / `BANNER_S3_SECRET_ACCESS_KEY` select credentials only for
banner clients. Both absent preserve the SDK credential chain; incomplete pairs raise a
configuration error at settings startup and before SDK client construction. The existing avatar
adapter, private bucket behavior, AES256 encryption, image validation and API schemas are unchanged.
No migration is added. The handoff includes a same-bucket B2 setup with a dedicated `banners/` key
and corrects the frontend pin to the owner's confirmed `88b26bea8e61f68adea4d2652b531f26cb5171d7`.

Executed affected command:
`pytest apps/content/tests apps/players/tests/test_avatars.py apps/players/tests/test_avatar_controls.py -q --tb=short`:
**52 passed in 15.53s**. This includes 10 new cases covering actual SDK credential isolation from
avatars, inherited session-token isolation, omitted credential arguments for IAM/shared-chain
fallback, environment fallback, both incomplete-pair directions at runtime and startup, no
credential values in startup errors, and exact stubbed private/encrypted PutObject/GetObject.
Only synthetic credentials and S3 stubs were used; no live B2 access or secret changes.

Ruff lint/format, mypy (**397** source files), Django system check, contract validator, both
OpenAPI drift checks and diff whitespace passed. Final follow-up commit SHA and its GitHub CI
results are recorded in Draft PR #28; earlier-head CI evidence above is historical.
