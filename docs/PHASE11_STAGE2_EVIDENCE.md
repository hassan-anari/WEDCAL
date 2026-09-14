# Phase 11 Stage-2 verification evidence

Evidence date: 2026-09-14 UTC. Canonical base: `0d1906c10ad2b94d6029f831937d79aba2cb192a`.
The final reviewed PR #16 HEAD is `eb6b293b472bacb273d69c8681406cc15af58c96`.
Owner-supplied hosted evidence proves PostgreSQL 17.11 migrations, Redis 8, RabbitMQ 4.3, no
migration drift, 542 passing tests, quality, Docker build/runtime, Admin/static smoke, and green
Contract Gate, Foundation, and Phase-11 Release Scans. The baseline Release Scans retained real
dependency-audit and container SARIF artifacts.

## Executed locally

- PostgreSQL 16.15, Redis 7.0.15, and RabbitMQ 3.12.1 were provisioned locally.
- The initial 528-test PostgreSQL/Redis/RabbitMQ regression run reproduced 10 legacy direct
  Season-transition failures. Tests now use a narrowly scoped fixture exercising the same
  transaction-local `dexo.season_transition` capability as production. The final regression result
  is recorded in the PR handoff; no production guard was weakened.
- Final hosted regression is **542 passed**.
- Real advisory-lock concurrency proved an exclusive close lock waits for an existing shared writer,
  the barrier marker commits afterward, subsequent writers fail closed, and Season lock keys do not
  cross-block.
- A complete six-epoch close campaign now exercises PREPARING through COMPLETED with a qualified
  Player, Clan economic source, confirmed DLX allocations, a held adjustment, same-category carry,
  final residual events, immutable Player/Clan standings, honors, and the CLOSED transition. An
  injected FREEZING_RANKINGS failure persisted the exact failed stage; resume incremented retry once
  and completed without duplicate ledgers. The pre-barrier reward appears in the final standing and
  a post-barrier reward fails closed. This campaign found and fixed non-JSON-safe Clan source-time
  provenance in epoch snapshots by encoding it as ISO-8601.
- Focused Player/Clan drift, immutable ranking, Telegram dead-letter, worker/redrive, outbox, and
  notification concurrency suites ran against PostgreSQL. Dead-letter semantic idempotency was
  hardened to include actor/service actor, outcome, reason, and evidence reference.
- Redis was flushed twice. Active loss/rebuild and atomic-swap checks pass, and new CLOSED/ARCHIVED
  Player plus CLOSED Clan tests prove rebuild reads immutable final standings and ignores late,
  unprocessed projection events.
- RabbitMQ was stopped while a Telegram update was durably inserted. Dispatch failed with
  `OperationalError`; after RabbitMQ restart, a real solo Celery worker processed a redriven update
  to `IGNORED`. The broker also contained stale task messages from transactional tests, demonstrating
  why PostgreSQL identity checks remain required. Together with the before/after-commit, stale-lease,
  duplicate-delivery, and hosted regression evidence, the Phase-11 recovery matrix is accepted.
- Two Beat contenders were started. The second exited `1` with
  `Another Beat owns the production singleton lock` while the first held the PostgreSQL advisory lock.
- Worker-loss tests prove rollback before commit leaves neither RewardLedger nor outbox state, while
  commit-before-ACK plus duplicate delivery converges to one ledger and one processed outbox. Existing
  stale-lease and concurrent-claim tests cover lease recovery and single ownership.
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
- A second complete economic-domain backup used the persisted end-to-end close dataset: two
  RewardLedger rows; Player progression/Season projection; Clan settlement, Treasury, Contribution,
  HQ XP and projection rows; six epochs; 30 DLX allocations; one held adjustment; five final category
  residuals; immutable Player/Clan standings; three medals; and Telegram dead-letter disposition
  evidence. Backup took 0.962 seconds and isolated restore took 1.984 seconds. Dump SHA-256 was
  `b392340e8bcb0b78a04dc56d21a02a348445723a70ab84da37e19a67131fe5b0`.
  Counts and MD5 checksums of every listed table matched source and restore exactly.

## Final container-security remediation

- The baseline SARIF contained 181 results, including three CRITICAL and 55 HIGH instances across 22
  unique HIGH/CRITICAL IDs. Fourteen IDs had upstream fixes; eight did not. The runtime now applies
  available Debian security updates. The final two Python findings were traced to pip's vendored
  CycloneDX inventory and vendored code in the official Python base, not installed distributions or
  `/app/.venv`; runtime pip is now removed after builder-only dependency installation. Trivy is
  commit-pinned and emits SARIF plus raw JSON; CI rejects every HIGH/CRITICAL with an available fix
  and retains the policy report.
- Final hosted Release Scans passed and retained Trivy SARIF, raw JSON, and policy artifacts. The
  result contains 44 HIGH/CRITICAL instances, 0 CRITICAL, 0 fixable HIGH, 0 fixable CRITICAL, and 8
  unique no-fix IDs. Each remaining ID has an accepted individual runtime disposition in
  `docs/PHASE11_CONTAINER_SECURITY.md`; PH-039 is accepted.

## Explicitly incomplete / owner action required

- Stage-2 economic correctness and recovery evidence is accepted at the hosted security-pass
  baseline. The narrow 50K measurement remains capacity evidence rather than a claim of 1M capacity;
  WATCH tuning does not reopen an accepted correctness gate.
- Branch protection is **OWNER ACTION REQUIRED**: require PRs, Contract Gate, and Foundation; block
  force pushes and deletion; keep emergency bypass time-limited and audited.
- PH-043 remains **OWNER PRODUCT DECISION REQUIRED**.

All application-controlled Phase-11 verification is complete. PH-037 and PH-043 remain explicit
repository-owner and product-owner launch actions.
