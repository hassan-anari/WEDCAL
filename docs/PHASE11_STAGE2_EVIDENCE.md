# Phase 11 Stage-2 verification evidence

Evidence date: 2026-09-14 UTC. Canonical base: `0d1906c10ad2b94d6029f831937d79aba2cb192a`.
The owner-provided approved Stage-1 hosted HEAD is
`22b905d1178e3bf603fbd4bd15eaf3870ade9914`. This workspace had no authenticated
GitHub remote; hosted checks and branch rules could not be queried or changed.

## Executed locally

- PostgreSQL 16.15, Redis 7.0.15, and RabbitMQ 3.12.1 were provisioned locally.
- The initial 528-test PostgreSQL/Redis/RabbitMQ regression run reproduced 10 legacy direct
  Season-transition failures. Tests now use a narrowly scoped fixture exercising the same
  transaction-local `dexo.season_transition` capability as production. The final regression result
  is recorded in the PR handoff; no production guard was weakened.
- Real advisory-lock concurrency proved an exclusive close lock waits for an existing shared writer,
  the barrier marker commits afterward, subsequent writers fail closed, and Season lock keys do not
  cross-block.
- Focused Player/Clan drift, immutable ranking, Telegram dead-letter, worker/redrive, outbox, and
  notification concurrency suites ran against PostgreSQL. Dead-letter semantic idempotency was
  hardened to include actor/service actor, outcome, reason, and evidence reference.
- Redis was flushed twice. The eight active-ranking loss/rebuild and atomic-swap tests passed. This
  is not evidence for the CLOSED/ARCHIVED final-standing-only path, which remains pending.
- RabbitMQ was stopped while a Telegram update was durably inserted. Dispatch failed with
  `OperationalError`; after RabbitMQ restart, a real solo Celery worker processed a redriven update
  to `IGNORED`. The broker also contained stale task messages from transactional tests, demonstrating
  why PostgreSQL identity checks remain required. Full process-kill timing permutations remain pending.
- Two Beat contenders were started. The second exited `1` with
  `Another Beat owns the production singleton lock` while the first held the PostgreSQL advisory lock.
- Production settings ran with `DEBUG=false`: live/readiness, Admin login, Admin CSS, and OpenAPI
  returned HTTP 200. Operations metrics returned 403 without its dedicated header and 200 with
  `X-Operations-Token`. `check --deploy` retained the intentional HSTS/SSL-redirect warnings because
  TLS termination belongs to the verified edge topology.
- `pip-audit` inspected 33 locked runtime dependencies and reported zero known vulnerabilities.
- A 50,000-Player PostgreSQL dataset was inserted in 0.955 seconds. The table occupied 9,306,112
  bytes including 2,965,504 index bytes. Indexed Telegram-ID lookup measured 0.279 ms execution time
  after `ANALYZE`. This is a narrow storage/query baseline, not an end-to-end capacity claim.
- The 50,000-Player database backup took 1.072 seconds and restore took 2.388 seconds. The custom
  dump SHA-256 was `a998b0e10351da9bd76ed3964f39d886886930f33f5c0781a0ca606184c786f6`.
  Source and isolated restore both reconciled to `50000|5001250025000|100000001|100050000` for
  Player count/sum/min/max. The drill did not contain the complete economic-domain dataset required
  for PH-031 acceptance.

## Explicitly incomplete / owner or hosted action required

- PostgreSQL 17 migration execution (the available package repository supplied PostgreSQL 16 only).
- Hosted Contract Gate, Foundation, Release Scans, actual Trivy container artifact, and PR HEAD.
- Docker image build and Compose runtime because no Docker-compatible runtime exists here.
- Full CLOSED/ARCHIVED Redis rebuild drill; worker death-before/after-commit matrix; full RabbitMQ
  notification/reward-outbox outage campaign; complete six-epoch end-to-end Season close; complete
  DLX drift campaign; and complete economic backup/restore checksum matrix.
- End-to-end scenario loads at 100K/250K/500K/1M. Manifests were generated, but only the narrow 50K
  Player database profile was executed. No production sizing or SLO conclusion follows from it.
- Branch protection is **OWNER ACTION REQUIRED**: require PRs, Contract Gate, and Foundation; block
  force pushes and deletion; keep emergency bypass time-limited and audited.
- PH-043 remains **OWNER PRODUCT DECISION REQUIRED**.

The incomplete items remain launch blockers. This document does not declare production readiness.
