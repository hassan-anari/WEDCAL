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
`DJANGO_ALLOWED_HOSTS`.
