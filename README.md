# Dexo Land Backend

This repository is the independent backend for Dexo Land. It is not an extension of the legacy Dalexio exchange backend.

## Required reading

Before making changes, read both repository contracts in full:

- [`AGENTS.md`](AGENTS.md) — development and agent governance rules.
- [`DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md`](DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md) — the canonical Season 1 product and backend contract.

## Phase-gated development

Every development phase uses its own pull request and must pass its required GitHub Actions checks. Green CI is necessary but is not approval to continue: the repository owner must explicitly approve progression. Coding agents must stop after a phase PR becomes green and must not start the next phase or merge a PR unless the owner explicitly authorizes it.

Branch names should identify the phase and slice, for example:

```text
ai/p00-contract-ci-gate
ai/p01-foundation-health
ai/p02-telegram-webhook
ai/p03-reward-ledger
```

## Phase 1 foundation

The foundation is a Django 5.2 ASGI application with a versioned Django Ninja API,
PostgreSQL as its only database, Redis as an ephemeral cache, and RabbitMQ as the Celery
broker. One application image is reused by the API, migration job, worker, and beat services.
No player or game-economy domain is included in this phase.

### Run locally with Docker

Python 3.13, Docker, and Docker Compose are the supported runtime toolchain. Copy the
development-only example configuration and start the complete stack:

```bash
cp .env.example .env
docker compose up --build
```

Compose runs the one-shot `migrate` service before starting `api`, `worker`, and `beat`.
PostgreSQL, Redis, and RabbitMQ have service health checks. Local infrastructure ports bind
only to `127.0.0.1`; the API is available on port 8000.

- Liveness: <http://localhost:8000/health/live>
- Readiness: <http://localhost:8000/health/ready>
- API documentation: <http://localhost:8000/api/v1/docs>
- Django admin: <http://localhost:8000/admin/>
- RabbitMQ management: <http://localhost:15672/>

Create an administrative user with Django's built-in staff authentication:

```bash
docker compose exec api python manage.py createsuperuser
```

Stop the stack with `docker compose down`. Add `--volumes` only when local PostgreSQL,
Redis, and RabbitMQ state should also be discarded.

### Local quality and test commands

Dependencies are locked in `uv.lock`; installs must remain frozen:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py migrate --noinput
uv run pytest
```

The database, cache, and broker integration tests require PostgreSQL, Redis, and RabbitMQ.
The easiest local test environment is the Compose infrastructure. The project deliberately
does not fall back to SQLite.

To apply formatting intentionally, run `uv run ruff format .`. Never commit `.env`; production
uses `dexo_land.settings.production` and refuses to load without `DJANGO_SECRET_KEY` and
`DJANGO_ALLOWED_HOSTS`, complete `POSTGRES_*` connection settings, `REDIS_URL`, and
`CELERY_BROKER_URL`. Runtime entry points also require `DJANGO_SETTINGS_MODULE` explicitly;
only `manage.py` defaults to local settings for developer convenience.

## Phase 2: Telegram identity and durable ingestion

Phase 2 adds two deliberately separate modules: `apps.players` owns the canonical Player
identity, while `apps.telegram` owns Telegram authentication, transport state, normalization,
groups, memberships, and the durable inbox. It does not score messages or implement referrals,
Clans, rewards, seasons, missions, or any other game economy.

### Telegram configuration

Configure these environment variables before accepting Telegram traffic:

- `TELEGRAM_BOT_TOKEN`: bot token used only to verify Mini App `initData`; it is mandatory in
  production and must never be logged.
- `TELEGRAM_WEBHOOK_SECRET`: secret configured with Telegram's webhook and required in the
  `X-Telegram-Bot-Api-Secret-Token` header; it is mandatory in production.
- `TELEGRAM_BOT_ID`: optional expected numeric identity for `my_chat_member` observations.
  Mismatching observations are ignored rather than mutating group state.
- `TELEGRAM_INIT_DATA_MAX_AGE_SECONDS`: maximum accepted Mini App authentication age.
- `TELEGRAM_INGRESS_RETENTION_SECONDS`: lifetime of sensitive raw inbox payloads. The
  `apps.telegram.tasks.purge_expired_telegram_payloads` Celery task clears expired terminal
  payloads while retaining update IDs and processing audit state. Pending and failed recovery
  payloads are retained.
- `TELEGRAM_INGRESS_PURGE_INTERVAL_SECONDS`: Celery Beat interval that automatically runs the
  ingress payload purge task.

The Mini App sends raw, unmodified Telegram `initData` to:

```text
POST /api/v1/auth/telegram
```

No separate Telegram user ID is accepted as proof of identity. The webhook endpoint is:

```text
POST /api/v1/telegram/webhook
X-Telegram-Bot-Api-Secret-Token: <configured secret>
```

The webhook verifies the secret, validates `update_id`, commits the unique PostgreSQL inbox row,
then publishes its database ID to RabbitMQ. A publication failure returns HTTP 503 but leaves the
pending row durable; Telegram's retry reuses and redispatches it. Workers lock and process each row
transactionally, so duplicate deliveries or worker retries produce one logical durable result.
Unknown update types are recorded as ignored.

Private `/start`, valid Mini App authentication, and a non-bot message in an explicitly monitored
group are the only Player first-seen paths. Monitoring is enabled internally through
`apps.telegram.services.set_group_monitoring`; merely observing a group does not enroll its users.
`chat_member` records Telegram's observed membership without creating a Player. Telegram group
membership is transport state only and is **not** future Dexo Land Clan membership or an invite.

### Local Telegram tests

Start the Compose dependencies, apply migrations, and run the focused suite against PostgreSQL:

```bash
docker compose up -d postgres redis rabbitmq
uv run python manage.py migrate --noinput
uv run pytest apps/telegram
```

The concurrency tests use independent database connections and therefore require PostgreSQL; the
project intentionally has no SQLite fallback. Raw Telegram inbox payloads are operationally
sensitive and are neither returned by public APIs nor displayed in Django admin.

## Phase 3 economic and progression foundation

Phase 3 adds explicit `DRAFT`, `ACTIVE`, `CLOSED`, and `ARCHIVED` Season records. A
PostgreSQL partial unique constraint permits at most one active Season, and reward settlement
also requires the event time to be inside its half-open `[starts_at, ends_at)` window. Season
activation is an explicit service/admin operation; automatic transition and season close are
not included.

One append-only `RewardLedger` row is the canonical point event. Its `player_amount` updates
both the persistent Lifetime Points projection and that event's `PlayerSeason.season_points`
projection in one database transaction. Season changes never reset or copy points into
Lifetime Points. Globally unique idempotency keys make matching retries safe; reuse with a
materially different player, source, context, amount, Season, or config version is rejected.
PostgreSQL row locks serialize a player's projection updates, and reconciliation can rebuild
both projections solely from the ledger.

`GameConfigVersion` stores complete effective-dated snapshots. Publishing validates integer,
ascending Level and Dexo thresholds and records a canonical checksum. Published identity,
effective time, and payload are immutable. Level, integer progress-to-next-Level values, and
Dexo stage are derived from Lifetime Points and the effective snapshot; stale per-player
derived projections refresh lazily without rewriting the ledger.

Protected Mini App reads use `Authorization: tma <raw Telegram initData>`. The server verifies
Telegram's signature and age and resolves the canonical Player; no client-supplied Player ID
is accepted. `GET /api/v1/bootstrap` returns only current Player, progression, active Season,
and safe config-version state. `GET /api/v1/rewards/history` provides bounded, stable,
Player-private ledger pagination without config payloads, idempotency keys, or metadata.

Season-close orchestration, ranking freeze, and final DLX allocation are explicitly not part
of Phase 3. Daily, Mission, Typing, Referral, Clan, Treasury, and ranking reward products are
also deferred to their approved later phases.

## Phase 4 Daily, Missions, and Typing

Phase 4 consumes the immutable `daily`, `missions`, and `typing` namespaces from the effective
`GameConfigVersion`; reward values and thresholds are never service constants. Daily and Typing use
a shared, timezone-aware reset-period calculator with calendar/DST semantics. Daily permits one
claim per Player/reset period. Its seven-entry reward cycle wraps independently while the actual
consecutive streak continues beyond day seven; missing a complete period resets that streak. The
claim record and canonical `DAILY` RewardLedger settlement commit atomically.

Missions use versioned definitions and per-player eligibility periods. Their lifecycle separates
`IN_PROGRESS`, `REWARD_READY`, and `CLAIMED`, so verification grants no points. `INTERNAL`
verification is fail-closed and currently proves `typing-session` from backend-owned Typing state;
`TELEGRAM` calls Bot API `getChatMember` with the authenticated Player and configured chat;
`TIMED_EXTERNAL` uses an opaque, hashed token and server-observed dwell time. Reward-ready rows
snapshot their source Season, config version, reward, and verification time before an idempotent
canonical `MISSION` claim.

Typing phrases and start/deadline timestamps are backend-owned and versioned. Submission accuracy
is NFC-normalized Levenshtein similarity in integer basis points. WPM is
`floor(target_character_count * 12 / server_elapsed_seconds)` and ignores extra submitted text for
speed. Config thresholds classify completed runs as valid, suspicious, or rejected; suspicious
results do not ban or credit. The config-driven daily completed-attempt quota uses the shared reset
period. Valid rewards append a Typing wallet credit rather than Player Points. A claim locks the
Player and source-Season wallet, groups unclaimed credits by original config version, atomically
creates canonical `TYPING_CLAIM` RewardLedger settlements and an append-only debit, and never
re-prices or moves credit to another Season. Wallet projections can be rebuilt from transactions.

Authenticated routes (all require `Authorization: tma <raw Telegram initData>`):

- `GET /api/v1/daily` and `POST /api/v1/daily/claim`
- `GET /api/v1/missions`, plus `/{mission_key}/start`, `/verify`, and `/claim`
- `GET /api/v1/typing`, `POST /attempts/start`, `POST /attempts/{attempt_id}/submit`, and
  `POST /claim` (start and claim require `Idempotency-Key`)
- `GET /api/v1/bootstrap` includes compact Daily, Mission, and Typing summaries.

Phase 4 does not implement referrals, activity scoring, Creator economy, Clans/Treasury,
achievements/collection, rankings, notifications, season close/Typing auto-settlement, DLX,
on-chain wallets, War/Defence, X OAuth, or manual point adjustment.
