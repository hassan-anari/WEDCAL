# B1 backend — Foundation & Telegram Authentication

Delivery unit: **B1 / P-01 + P-04**, repository `hassan-anari/WEDCAL`, branch
`ai/b1-backend-foundation`. Draft delivery; independent review and owner approval
are required. B1 is not accepted until the frontend and paired integration checks
pass. No B2–B6 work or merge is authorized by this handoff.

## Pinned context and environment

- Backend remote `main`: `f52a6555f0ba4b140df8d14ae578470bc533bb3a`.
- Frontend remote `main`: `6e9f3c7136f92fef4167ed2b490b847dff7e2bb7`.
- Read current AGENTS, Season 1 Source of Truth, Economy Config Contract,
  integration contract, relevant hardening backlog, authentication/provisioning,
  Bootstrap/progression/player/season/config, middleware, initial API routes and
  tests. Read PR #25; it has merged and makes B1 one batch rather than two gates.
- The local checkout began at stale `77ae622`. Every tracked blob was compared
  with the connector's recursive remote tree. Application, dependency and CI
  blobs matched; only AGENTS and the integration document needed synchronization.
  The synchronized local Git tree equals remote tree
  `b240bc9adef079b7b3f79b32482afd62649e7600`. The exact remote commit was restored
  from its signed GitHub commit metadata and verified by its Git object hash.
- Authorized GitHub connector access works for both repositories. Shell Git and
  unauthenticated HEAD attempts to `api.dalexio.com/health/live` and `dalexio.com`
  failed at proxy port 8080. No production response or reachability was verified.
  The runtime policy snapshot also did not list those custom domains. No proxy
  bypass, authenticated production request, player creation, reward, mutation or
  load test was attempted against production.
- Locked `uv sync --frozen` succeeds using the existing environment dependencies.
  The sandbox denies local socket creation (`EPERM`); database/service tests must
  run in GitHub Actions. Local migration drift detection says no changes, but its
  database-history check could not connect. This is not PostgreSQL test evidence.

## Confirmed backend findings

1. A non-ASCII supplied hash reaches `hmac.compare_digest(str, str)` and raises
   `TypeError`, producing a 500 rather than an authentication failure. Validate
   the SHA-256 hex encoding first; constant-time HMAC comparison remains intact.
2. Signed user IDs were coerced through `int()`, accepting booleans/floats/string
   IDs and potentially overflowing PostgreSQL identity/advisory-lock storage.
   Require a positive JSON integer fitting the existing signed bigint storage.
   This was malformed signed-data acceptance, **not proof of an unsigned bypass**.
3. Invalid timestamp platform errors and signed profile values exceeding model
   limits or containing NUL could become server errors. Reject those inputs before
   provisioning. Actual signed Unicode profile text remains supported.
4. Both auth transports collapsed expiry and missing server bot configuration into
   an undifferentiated 401. Distinguish credential failures from service outages
   with a stable, sanitized error envelope; preserve a string `detail` field.
5. Bootstrap exposed `dict[str, Any]` in OpenAPI. Publish nested response schemas
   and additive config/feature/gameplay/request metadata.
6. Lifetime progression was read in a transaction that ended before Season Points
   were read. A reward could commit between the reads. Hold the canonical Player
   row lock until the compact snapshot is built. Reward settlement uses the same
   lock before both projections. No reward/accounting implementation changes.
7. Bootstrap/auth responses now explicitly disallow shared caching and vary on
   Authorization, supporting account isolation through the frontend proxy.

Canonical provisioning already has a unique Telegram ID, transactional upsert,
Player row lock and PostgreSQL advisory lock with membership/candidate backfill.
Retain those mechanisms. Add simultaneous signed-launch API coverage on independent
PostgreSQL connections, including exactly one `created: true` response.

## Frontend findings and VPN limits

At the pinned frontend commit, `index.html` depends on
`https://telegram.org/js/telegram-web-app.js`. The adapter freezes missing SDK/launch
context as browser mode, while the client says to open Telegram when signed data
is absent. Onboarding calls Bootstrap without `adapter.getInitData()`. Query keys
for Bootstrap/Profile/Notifications/DLX are not scoped by authenticated player and
season. The Bootstrap adapter discards rich server state and hardcodes Clan level
5 / Arena unavailable.

Those are confirmed **frontend code findings**, not a reproduced VPN-off root
cause. The backend does not determine whether the SDK loaded or whether the user
opened Telegram. No backend network, CORS or signature workaround is justified
by this hypothesis. Actual Android/iOS/Desktop VPN-off/on WebView recordings,
SDK/origin/API reachability and cold-launch timing remain frontend/paired gates.

`vercel.json` uses a same-origin `/api/v1/*` rewrite to the production API;
no direct cross-origin browser API contract is introduced. Production settings
already require bot credentials, allowed hosts, database and operational settings;
forwarded HTTPS trust is explicitly opt-in. Deployment ingress must preserve
Authorization, exclude personalized API data from caches, keep clocks synchronized,
and trust forwarded headers only from its controlled proxy. No deployment settings,
repository names or remotes are changed here.

## API contract 1.1 (backward-compatible additions)

Routes remain `POST /api/v1/auth/telegram` and `GET /api/v1/bootstrap`.
Protected routes continue to use `Authorization: tma <raw signed initData>`;
scheme case and separator whitespace are now handled consistently. Auth POST still
accepts `{ "init_data": "..." }` and returns its existing Player fields plus
`created`. It does not issue a cookie, bearer token or new session credential.
Every protected request independently verifies the signed launch and its age.
Neither user IDs, request IDs, `initDataUnsafe`, query parameters nor proxy headers
authenticate the caller. Replaying a valid launch within its configured TTL is
permitted and resolves the same Player.

All existing Bootstrap fields and nullable feature summaries remain. The existing
frontend Zod schema accepts additive properties. The new fields are:

| Field | Type / meaning |
|---|---|
| `contract_version` | Literal `"1.1"` for this additive contract |
| `request_id` | Safe correlation ID, identical to `X-Request-ID`; not a principal |
| `server_time` | UTC ISO timestamp used to build this snapshot |
| `player.id` | Stable string representation of verified `telegram_user_id`, matching the current frontend key |
| `player.language_code`, `player.photo_url` | Safe profile strings; absent values remain empty strings |
| `config.schema_version`, `config.payload_checksum` | Identity of the published immutable config payload; `version` and `effective_at` remain |
| `config.progression.levels` | `{level, min_lifetime_points}[]` from the effective config |
| `config.progression.dexo_stages` | `{key, min_level}[]` from the effective config |
| `config.progression.clan_min_level` | Configured threshold, or null when Clan policy is absent |
| `features` | Boolean `season`, `daily`, `typing`, `missions`, `referral`, `network`, `base_tap`, `clan`, `profile`, `arena` |
| `gameplay.moderation_state` | `ACTIVE`, `FLAGGED`, `SUSPENDED`, `BANNED` |
| `gameplay.reward_earning_allowed` | Active economic window and canonical moderation eligibility |

Feature flags describe configured/read availability. Daily/Typing/Missions match
their nullable summaries; Clan additionally requires its configured level gate.
They are not authorization grants or proof that the frontend integration has been
released. `reward_earning_allowed` is a separate general gameplay gate; individual
actions still enforce their own eligibility/quota/content policy. Suspended/banned
players can authenticate and read their history. FLAGGED does not mean banned.
No active Season is a valid 200 response with `season: null`, zero initial lifetime
progression for a new player, and nullable season-dependent summaries.

Error envelope: `{ "detail": "safe message", "code": "...", "request_id": "..." }`.
The common auth errors apply to all MiniAppAuth endpoints, including existing
Profile, History, Notifications and Arena reads. Bootstrap and auth POST declare
their errors explicitly in OpenAPI. Other endpoint-specific business errors retain
their existing response contract.

| HTTP | Code | Meaning / client action |
|---|---|---|
| 401 | `AUTH_MISSING` | Credentials missing; recover signed launch data |
| 401 | `AUTH_INVALID` | Bad scheme, malformed/tampered data, invalid signature/date/user; recover/relaunch |
| 401 | `AUTH_EXPIRED` | Signature verified but launch age expired; reopen Mini App |
| 503 | `AUTH_UNAVAILABLE` | Server bot verification configuration unavailable; retry later |
| 503 | `INITIALIZATION_UNAVAILABLE` | Provisioning database or Bootstrap config/initialization unavailable; bounded retry |
| 400/422 | `REQUEST_INVALID` | Auth POST has malformed JSON / invalid body schema |

Auth/init errors never include raw credentials or internal database/config errors.
Bootstrap/auth success and error responses use `Cache-Control: private, no-store`
and `Vary: Authorization`.

## Frontend B1 integration requirements

1. Send raw signed initData on every initial protected fetch, including onboarding
   and cold deep links. Do not fall back to unsigned identity.
2. Parse `code` as well as HTTP status and `detail`. SDK/data recovery, transport
   failures/timeouts and backend 401/503 are separate states. Do not label a 503,
   missing SDK or blocked origin as proof the user is outside Telegram.
3. Use server player/config/season identities to scope caches; clear prior-account
   data immediately when launch identity changes, before a new response arrives.
   Do not put raw credentials into query keys, telemetry or persistent stores.
4. Retain typed rich summaries, server feature/gameplay flags and progression
   thresholds. Do not invent season/lifetime rewards or fixed Clan gates locally.
5. Validate a pinned frontend/backend pair in an isolated harness or staging before
   rollout, including valid launch, reload, account switching, absent Season,
   config turnover, auth expiry, offline/timeout and recoverable initialization.
6. Complete frontend test discovery/CI and the real Telegram VPN matrix. No B1
   frontend implementation or cross-repo E2E acceptance is claimed by this PR.

## Validation and delivery evidence

Local commands use the supported lock via `UV_CACHE_DIR=/workspace/work/uv-cache`:

- `uv sync --frozen`: pass.
- `uv run pytest apps/telegram/tests/test_auth.py apps/telegram/tests/test_b1_auth.py apps/rewards/tests/test_b1_bootstrap.py -m 'not django_db'`:
  71 passed; PostgreSQL-marked tests explicitly excluded locally.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`,
  `uv run python manage.py check`, `uv run python scripts/validate_contract.py`: pass.
- `uv run python manage.py makemigrations --check --dry-run`: no changes;
  database history connectivity warning, not migration execution evidence.

Existing `Phase 1 Foundation` PR CI runs locked quality checks, real PostgreSQL 17,
Redis 8 and RabbitMQ tests, migrations and Docker build/smoke. Added tests require
PostgreSQL rather than substituting SQLite. They cover simultaneous signed launches,
real reward/Bootstrap lock contention, identity isolation, config turnover, max
level/null thresholds, no-Season/disabled/moderated states and OpenAPI contracts.
CI URLs and results are recorded in the draft PR after execution. Passing local
tests alone does not satisfy the PostgreSQL or paired frontend acceptance gate.

## Deployment, rollback and remaining risk

No migrations, dependency changes, reward writers or economy/config publication
changes. Existing production clients keep their routes, successful response fields
and string `detail` errors. Expired/invalid credentials remain 401; missing server
configuration and transient initialization now correctly return 503. Additions do
not activate new gameplay. No new feature flag is needed for additive read metadata.

The Bootstrap Player lock can briefly delay concurrent rewards. Full fixture
concurrency is required in CI; realistic query/latency budgets and WebView/provider
tests remain unmeasured. This addresses PH-020's typed-contract portion, not its
realistic performance acceptance. Existing PH-037 branch protection and PH-043
Season Brief product decision remain owner gates. Do not claim launch readiness.

Rollback is a normal code revert through a separately reviewed PR after owner
authorization; it removes additive contracts/error handling and restores prior
read behavior. Provisioned players/progression are retained. No ledger/history
rewrite, pending obligation deletion, reward replay or schema rollback is involved.
The new frontend should tolerate missing additive fields while backend rollback
remains possible, or gate its rollout on this backend version.

Residual P1: frontend SDK/VPN launch reproduction and account-cache isolation;
paired B1 acceptance pending. Residual P2: realistic Bootstrap lock/query latency
measurement. No confirmed unsigned-auth bypass was found. Stop for independent
review after green backend checks; neither merge nor the next batch is authorized.
