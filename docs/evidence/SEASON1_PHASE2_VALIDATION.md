# Season 1 completion Phase 2 — validation record

Base `e11bdb3e3c85c75f86bbda3c5aeaa54889dc7dbf`; branch
`ai/season1-phase2-gameplay-completion`. Scope and operator procedure:
[gameplay handoff](../SEASON1_PHASE2_GAMEPLAY_HANDOFF.md).
The Draft PR records its actual final SHA and exact-head hosted check links; this local record is
not a substitute for those required checks or independent review.

## Independent defect reproduction

- New cross-Season ranking selector regression failed before the fix (1 failed, 12 passed).
  A prior-Season founding award plus a current-Season different medal incorrectly excluded the player.
  Only the same-award selector changed; percentile formula/tie rules/config stayed intact.
- New mission-disable and deactivation-before-ready regressions both failed before the fix
  (`DID NOT RAISE MissionUnavailable`). Verification now rechecks disabled state, including the
  definition row held by the ready-transition transaction. Historical ready claims remain preserved.
- Clan Loyalty and Leadership had no existing evaluator; the configured rules were present.
  New historical-evidence reconciliation completes those paths without economic mutation.

## Local verification

Commands run in WEDCAL with the frozen lockfile and isolated PostgreSQL 17.11, Redis and RabbitMQ.
`UV_CACHE_DIR=/workspace/.cache/uv` selects a workspace-owned tool cache in this environment.

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python manage.py check
uv run python manage.py migrate --noinput
uv run python manage.py makemigrations --check --dry-run
uv run python scripts/export_tap_v2_openapi.py --check
uv run python scripts/export_b3_openapi.py --check
uv run python scripts/export_b4_openapi.py --check
uv run python scripts/export_b5_openapi.py --check
uv run python scripts/export_b6_openapi.py --check
python scripts/validate_contract.py
git diff --check
```

All passed. mypy: 474 source files. Migration execution passed; no drift. Only new migration is
`profile_collection.0002_career_medal_evidence`; [rendered SQL](SEASON1_PHASE2_MEDAL_EVIDENCE.sql).
No published JSON examples, requirements, API fields or OpenAPI fixtures changed.

Focused final matrix:

```bash
POSTGRES_DB=phase2_final_focused uv run pytest \
  apps/missions/tests/test_completion_season_brief.py \
  apps/profile_collection/tests/test_completion_clan_medals.py \
  apps/clans/tests/test_completion_equipment_qa.py \
  apps/seasons/tests/test_completion_ranking_medals.py -q --tb=short
```

54 passed in 47.85 seconds, no failures or skips. The tie test was subsequently strengthened to
exercise the actual batched FREEZING_RANKINGS/AWARDING_HONORS stages with tied players on both
sides of the percentage boundary; its complete ranking file was rerun separately: **14 passed**,
29.90 seconds, no failures or skips.

Initial full local `uv run pytest -q --tb=short`: **1222 passed, 1 skipped, 5 warnings**, 667.66s.
That run preceded the final focused additions/verification-state fixes; it is recorded as initial
full-suite evidence, not falsely labelled final-head evidence. The final full suite must pass again
in GitHub Actions at the delivered SHA. The single deliberate skip is the existing B2 benchmark,
separately enabled and run as required:

```bash
B2_BENCHMARK=1 uv run pytest apps/base_tap/tests/test_benchmark.py -q --tb=short
```

**1 passed**, 75.86s. Synthetic 16-worker/64-player run: 512 accepted batches / 12800 taps,
513 ledger rows and 513 facts including the same-player contention case; that case produced exactly
one new reward and 15 replay receipts. Shared local Docker/test load affected timings. This is
correctness/benchmark execution evidence, not a production throughput or capacity claim.

## Frontend compatibility, with no tracked frontend changes

FROCAL `eb76307615322016bc39509c467f64be0268423a` inspected for EarnPage, ProfilePage,
ClanPage, useB3, useClanRemote, useServerProfile, profile schemas/catalog, ClanOverlays and
B3/B4/B5/B6 controller/browser tests. Existing generic external mission navigation and current
percentage medal copy/assets already match the owner decision.

```bash
npm ci --ignore-scripts
npx vitest run src/integrations/api/useB3.test.tsx \
  src/integrations/api/useClanRemote.test.tsx \
  src/integrations/api/b3.contract.test.ts \
  src/integrations/api/b4Contracts.test.ts \
  src/integrations/api/b5.contract.test.ts \
  src/integrations/api/b6.contract.test.ts
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium npx playwright test \
  --config playwright.b6.config.ts e2e/b6-integration.spec.ts \
  e2e/b6-async.spec.ts e2e/b6-history.spec.ts e2e/b6-activity.spec.ts
```

**87 API/controller tests passed; 23 intercepted browser tests passed.** No live API/player calls.
Browser-generated tracked screenshots were restored to their original bytes; frontend tracked diff
is empty. No frontend commit, branch publication, issue or comment was created.

Additional scratch-only Vitest harness: **9 passed**. It parses all seven newly generated backend
equipment/purchase/activation HTTP observations, new career medal ownership/customization response,
and official Season Brief list/start/claim response using the **unchanged** FROCAL schemas and
`missionView`. It is a local consumer compatibility check, not staging/Telegram acceptance.
Generated backend observations remain explicitly test-only in `work/phase2-*.json` and are
reproducible from the committed tests. No new API fields were needed.

## Container/runtime and remaining gates

```bash
docker build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt \
  --tag wedcal-phase2:review .
```

Exact Dockerfile build passed, including image-owned manifest static collection/Admin smoke.
Session CA is supplied as a build secret, never committed or baked into a new source file.
Isolated runtime smoke uses a fresh local PostgreSQL database and RabbitMQ vhost, with no Player
fixtures; API liveness/readiness 200, Admin login/static 200, worker `pong`, Beat running and writable
run directory. Containers are removed afterward. No Production connectivity or deployment.

Hosted Contract Gate, Foundation quality/test/docker-build and Release Scans dependency/container
checks are required at final HEAD; links/status are recorded in the Draft PR. Paired final-pin
staging/Telegram website-open/device acceptance and any approved live mission config publication
remain operator/reviewer work. No merge, deployment, automatic backfill, Phase 3 work or DLX
settlement activation was performed.
