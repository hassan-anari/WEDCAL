# AGENTS.md — Dexo Land Backend Agent Rules

**Scope:** repository root and all backend code unless a deeper `AGENTS.md` explicitly adds narrower rules.  
**Applies to:** Codex and every other AI coding agent, human contributors using agent-generated patches, PR reviewers.  
**Canonical product contract:** `DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md`

---

## 1. Required reading before editing

Before changing code, every agent MUST read:

1. `AGENTS.md`
2. `DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md`
3. the files/tests directly related to its assigned slice
4. the final frontend source/contract only when the task requires confirming a user-visible flow

The legacy Dalexio backend may be consulted for implementation patterns only. It is not the product contract.

Do not code from memory of earlier conversations.

---

## 2. Source-of-truth rule

The final frontend defines Season 1 product scope/user-visible states. The English Source of Truth records the approved backend/business rules that fill gaps not expressible in the frontend.

If old docs, old backend code, diagnostics, comments or conversations conflict with the approved Source of Truth, the approved Source of Truth wins.

Do not resurrect removed concepts such as:

- Officer role
- Season 1 War/Defence/Rivalry
- exchange/whitelist coupling
- old mutable airdrop point logic
- fixed demo economy values as permanent rules

---

## 3. Hard prohibitions

An agent MUST NOT, unless the user explicitly changes the contract:

- implement War, Defence, Rivalry, Alliance or World Events backend logic in Season 1;
- add an Officer/Vice-Captain role;
- make Dexo Land depend on the old exchange/site database or account model;
- add whitelist requirements;
- implement wallet/on-chain withdrawal in Season 1;
- hardcode configurable reward/threshold/economy values inside domain services;
- treat frontend sample numbers as permanent economics;
- use Redis as the only durable store for points, Treasury, membership or claims;
- perform full scoring/business logic synchronously in the Telegram webhook;
- create reward mutations without idempotency/audit semantics;
- trust client-supplied Telegram IDs, Clan ownership, scores, rewards or admin claims;
- implement `player.points += x` as the sole authoritative accounting mechanism;
- use Redis `KEYS` in production workflows;
- delete queued/buffered economic data before it is durably persisted;
- hide a business-rule assumption inside a migration, constant or test fixture;
- make unrelated architecture/refactor changes in a feature PR;
- edit the Source of Truth to justify implementation choices unless the task explicitly asks for a contract update.

---

## 4. Config-first rule

If a mechanism is specified but the numeric value is not finalized, implement the mechanism using versioned configuration.

Examples:

- reward amounts
- level curves
- message/minute limits
- max message characters
- typing formula
- referral tiers
- Treasury percentage
- Captain bonus
- Clan capacity
- inactivity thresholds
- leave cooldown
- governance quorum/approval percentage
- message retention duration

Allowed:

```python
policy.max_messages_per_minute
policy.treasury_rate
policy.typing_attempt_limit
```

Not allowed:

```python
if messages > 5: ...       # hidden domain constant
TREASURY_RATE = 0.10       # permanent business rule in service module
```

Development/test seed values may mirror frontend examples, but label them clearly as sample configuration and keep domain code independent of them.

---

## 5. Multi-agent coordination rules

Multiple agents may work in parallel, but only on slices with clear ownership.

### 5.1 One agent = one scoped slice

Every agent must state its scope before editing, for example:

```text
Phase 2 / Telegram webhook dedupe only
Files/domains expected: apps/telegram, tests/telegram
No reward logic changes
```

Do not expand scope silently.

### 5.2 Avoid overlapping migrations

Two agents MUST NOT independently create migrations for the same Django app/domain at the same time unless their dependency/order is explicitly coordinated.

Migration conflicts are expensive and easy to miss.

Preferred parallelism:

- one agent on Telegram ingestion,
- one agent on CI/test infrastructure,
- one agent on documentation/schema tests,

rather than two agents both editing `rewards/models.py` and creating competing migrations.

### 5.3 Shared core files

Files such as settings, root URLs, shared config primitives, base models and central dependency files are high-conflict zones.

An agent modifying a high-conflict file should keep the change minimal and call it out in the PR description.

### 5.4 Rebase before handoff

Before finalizing a branch/PR:

- rebase/update from the current target branch,
- resolve conflicts intentionally,
- rerun the full relevant test suite,
- inspect migration graph after rebase.

### 5.5 Do not combine agent outputs blindly

Never concatenate patches from multiple agents without running tests and reviewing overlapping logic.

If two implementations solve the same contract slice differently, choose one intentionally; do not keep both.

---

## 6. Branch and PR discipline

Prefer small PR-sized slices.

### 6.1 Mandatory phase delivery gate

Every development phase is a hard-gated delivery unit.

Required flow:

```text
implementation
→ dedicated pull request
→ GitHub Actions
→ every required check green
→ repository owner review
→ independent PR/CI review
→ explicit owner approval
→ only then may the next phase begin
```

- Every phase, including Phase 0, MUST have GitHub Actions coverage appropriate to its scope.
- Every phase MUST use a dedicated pull request.
- A pull request with failing, cancelled, missing or required-skipped checks is incomplete.
- CI failures must be fixed inside the same phase and pull request.
- Passing CI is necessary, but it is not sufficient to begin another phase.
- After the pull request's required checks become green, the coding agent MUST STOP.
- The coding agent MUST NOT begin the next phase automatically.
- The next phase may start only after the repository owner explicitly authorizes it.
- Agents MUST NOT merge pull requests unless the repository owner explicitly asks them to.

The owner provides the green pull request URL to an independent reviewer before authorizing merge or the next phase.

Recommended branch naming:

```text
ai/p01-foundation-health
ai/p02-telegram-webhook
ai/p03-reward-ledger
ai/p07-clan-membership
```

A PR description should state:

- contract section(s) implemented,
- scope/non-scope,
- migrations added,
- API schema changes,
- concurrency/idempotency behavior,
- tests added/run,
- config values introduced,
- known follow-ups.

Do not submit giant cross-phase PRs.

---

## 7. Architecture boundaries

Dexo Land starts as a modular Django monolith.

Business rules belong in domain/application services, not API routers and not Celery task bodies.

Preferred shape:

```text
Ninja router
→ input schema validation/auth
→ application/domain service
→ transaction/repository/ORM
→ result schema
```

Celery:

```text
Celery task
→ deserialize identifiers
→ call application/domain service
→ retry only according to explicit policy
```

Do not place hundreds of lines of business logic in a router or task.

---

## 8. Domain dependency direction

Keep dependencies explicit.

Examples:

- Telegram ingestion emits normalized domain events.
- Reward service settles canonical point events.
- Referral service reacts to eligible settled reward events through an explicit application workflow/event, not by importing random model internals.
- Clan Treasury split is part of an explicit Clan-context settlement, not a signal with hidden side effects.
- Rankings consume durable score projections and publish Redis projections.

Avoid circular app imports and Django signals for critical economic workflows when an explicit service call/outbox/event is clearer and more testable.

---

## 9. Telegram rules

### 9.1 Authentication

- Verify Mini App `initData` server-side.
- Never accept a plain Telegram user ID as proof of identity.

### 9.2 Webhook

Webhook responsibilities are limited to:

- verify secret,
- dedupe update,
- normalize/persist/enqueue,
- fast ACK.

Do not score messages in the request path.

### 9.3 One bot

Do not add multi-bot abstractions unless required by a later contract.

### 9.4 Silent player creation

Player may first appear from:

- `/start`
- Mini App
- group message

All must resolve to one unique Player by Telegram user ID.

### 9.5 Group/Clan state

Telegram Group membership, Dexo Land Clan membership and invite links are separate state machines.

Never delete Clan membership solely because one Telegram membership event says `left/kicked`.

---

## 10. Reward and point accounting rules

### 10.1 Canonical point event

One eligible point event contributes to:

- active Season Points projection,
- Lifetime Points projection.

Do not mint two separate point events for those views.

### 10.2 Ledger required

Every player reward must be traceable to a durable source/idempotency key.

### 10.3 Config version

When calculations depend on configurable economy, record the effective config version/reference.

### 10.4 No double lifetime settlement

Season close must not add Season Points into Lifetime again.

### 10.5 XP and confirmed DLX allocation are separate

Season 1 creates confirmed, auditable, off-chain DLX allocations through six weekly economic epoch settlements. Confirmed allocation is not claimable, transferable, tradable, or on-chain. Keep DLX allocation out of the XP `RewardLedger`; it must not change Level, Lifetime Points, or Season XP and must not recursively create rewards.

---

## 11. Typing rules

- Daily attempt limit is config (current product default concept: 5).
- Reward is variable based on accuracy/speed and config.
- Eligible attempt reward credits Typing Claimable Balance first.
- Player may claim after any number of attempts.
- Claim atomically converts current claimable balance to canonical point reward and debits typing wallet.
- Claim/attempt submission must be idempotent and concurrency-tested.
- Do not require all daily attempts to be used before claim.

---

## 12. Mission rules

Required Season 1 verification strategies only:

- `INTERNAL`
- `TELEGRAM`
- `TIMED_EXTERNAL`

For timed external missions, backend records start/open state and verifies elapsed server time when the app later resumes/syncs.

Do not invent OAuth/provider verification unless explicitly tasked.

---

## 13. Activity / anti-fraud rules

Network Group and Clan Home activity use a shared engine with separate policy/config.

Contexts:

```text
NETWORK_GROUP
CLAN_HOME
```

There is no generic daily conversation cap.

Use configurable controls such as rate, max rewardable characters, duplicate detection and suspicious-pattern rules.

Activity can be classified valid/rejected/suspicious/flagged.

A flag is not automatically a ban.

Do not implement silent irreversible punishments without an auditable state/action.

---

## 14. Network Creator/Speaker rules

- Bot Admin required for reward-producing Creator Group.
- Creator reward owner is explicit.
- Qualified activity by other community members drives Creator Program value.
- Speaker personal reward capability must exist.
- Initial normal Network Speaker reward is disabled/zero via config.
- Do not port V1's automatic direct speaker reward as an always-on rule.

---

## 15. Clan rules

### 15.1 Roles

Season 1: `CAPTAIN`, `SOLDIER` only.

### 15.2 Persistent vs seasonal

- `Clan` persistent identity.
- `ClanSeason` seasonal progression/competition.
- memberships/roles are season state.

### 15.3 Telegram eligibility

Captain registration must verify:

- user authority in group,
- bot Admin state.

No manual Chat ID input.

### 15.4 Accepted join

Captain accept → user-specific invite → Bot DM → Telegram join verification → ACTIVE membership.

### 15.5 Access loss

Telegram leave/kick → `ACCESS_LOST`, not immediate Dexo membership deletion.

### 15.6 Voluntary leave

Allowed with configurable friction/cooldown. No direct instant switch.

Historical Contribution stays with old Clan.

### 15.7 Captain removal power

Captain does not get an unrestricted immediate backend kick.

Ordinary community removal uses a removal case + timed governance.

Serious safety/fraud cases use platform moderation.

### 15.8 Telegram ban bypass

If Captain directly bans a Soldier in Telegram without approved removal:

- mark access lost,
- create incident,
- do not treat it as approved Dexo Land removal,
- run governance/moderation/recovery flow.

### 15.9 Inactivity

Soldier: warning → reminder → configurable inactivity removal.

Captain: warning → Recovery Mode → timed dissolution/release governance.

No Captain succession in Season 1.

### 15.10 Contribution

Captain participates in Contribution normally.

Contribution history is immutable by later Clan switching.

### 15.11 Treasury

For configured Clan activity, player/Treasury split is atomic and configurable.

Contribution is based on gross eligible Clan activity before Treasury split unless the contract later changes.

### 15.12 Defence

Do not activate/show Season 2 Defence item catalog/effects. Only generic extensible item infrastructure is allowed.

---

## 16. Moderation and message evidence

Because user reports can involve message content, the system may retain reward-relevant Telegram message text for a configurable limited retention period.

- raw content is not a permanent analytics dataset by default;
- Moderation Case evidence may be retained longer;
- admin access is permissioned/audited;
- bans/suspensions do not erase history;
- economic adjustments must be explicit ledger adjustments.

---

## 17. Database transaction rules

For money-like/game-value mutations, prefer database-enforced correctness.

Use:

- unique constraints,
- transactions,
- `select_for_update` where justified,
- optimistic/version checks where appropriate,
- append-only ledgers,
- deterministic idempotency keys.

Do not rely only on Redis locks for correctness.

Redis locks may reduce contention but database constraints remain the final safety layer.

---

## 18. Redis rules

Redis is for:

- cache,
- rate limiting,
- ephemeral state,
- rankings,
- temporary coordination.

Redis is not the only copy of:

- player points,
- typing claimable accounting history,
- Treasury ledger,
- Clan membership,
- moderation/governance decisions.

Never use `KEYS` in production request/worker paths.

Ranking Redis state must be rebuildable from PostgreSQL.

---

## 19. RabbitMQ / Celery rules

RabbitMQ is the broker.

Celery tasks should be small orchestration wrappers.

Every task must define retry semantics intentionally.

External Telegram/API calls require:

- timeouts,
- bounded retries,
- handling for rate limits,
- safe logging.

A retry must never duplicate reward settlement.

---

## 20. API rules

Use Django Ninja schemas for input/output contracts.

Do not return raw Django models or internal exception text.

For mutations:

- authenticate/authorize server-side,
- validate current state,
- run domain service,
- return deterministic state/result.

Any API change that affects frontend integration should update OpenAPI/schema tests and relevant contract docs/comments.

---

## 21. Migration rules

- Never edit an already-applied migration unless the repository is explicitly pre-release and the task says to squash/reset.
- Keep migrations narrowly scoped.
- Add database constraints/indexes that enforce core invariants.
- Review migration SQL/locks for large tables.
- Data migrations must be restartable/idempotent where practical.
- Before PR handoff run migration consistency checks.
- Multi-agent branches must coordinate migration dependencies.

---

## 22. Testing rules

Every bug fix should include a regression test when feasible.

Every critical economic/state transition requires tests for:

- happy path,
- duplicate/retry,
- unauthorized call,
- invalid state,
- concurrency where relevant.

Critical examples:

- daily claim
- mission claim
- typing submit/claim
- referral verification/share
- creator activity settlement
- Clan role/membership
- invite acceptance
- voluntary leave
- removal/governance vote
- Treasury purchase/split
- item activation
- ranking rebuild

Do not mock away the transaction behavior you are trying to test.

---

## 23. Code quality rules

- Prefer explicit names over clever abstractions.
- Keep functions small enough to test.
- Use typed Python interfaces where the project tooling supports them.
- Avoid premature generic frameworks.
- Do not introduce microservices.
- Do not add a repository layer solely for fashion if Django ORM services remain clear/testable.
- Do not add dependencies without a concrete need.
- Reuse standard Django/PostgreSQL functionality before custom infrastructure.
- Keep comments for why/invariants, not restating obvious code.

---

## 24. Security rules

Never:

- commit secrets,
- log bot tokens,
- log Telegram initData hashes/secrets,
- trust client ownership/admin state,
- expose moderation message logs to normal users,
- expose internal numeric IDs when unnecessary,
- skip permission checks because the frontend hides a button.

Security-sensitive changes must include negative authorization tests.

---

## 25. Observability rules

New asynchronous/domain workflows should emit enough structured context to debug them:

- correlation/source event ID,
- player ID (internal, safe form),
- group/Clan ID where relevant,
- task name/attempt,
- outcome/classification,
- error category.

Do not log full sensitive message content in general application logs. Message evidence belongs in controlled storage.

---

## 26. Performance rules

Before optimizing, preserve correctness.

Then:

- prevent N+1 queries,
- add indexes for actual lookup paths,
- batch high-volume activity work,
- avoid per-character database writes,
- keep webhook path minimal,
- use Redis projections for ranking reads,
- paginate list endpoints,
- bound admin/broadcast queries.

Any optimization that changes reward semantics requires tests proving equivalent results.

---

## 27. Legacy backend reuse checklist

An agent may port/refactor old Dalexio Airdrop code only after verifying it does not import exchange-domain assumptions.

Good candidates:

- Telegram auth helper logic
- webhook secret handling
- update dedupe
- first-touch referral attribution
- Telegram membership/admin parsing
- Telegram request wrapper
- leaderboard rebuild logic
- concurrency tests

Bad candidates to copy directly:

- old Airdrop models
- direct mutable profile scores
- old buffer delete-before-persist pattern
- Redis KEYS scan
- exchange subscription/balance code
- whitelist/account link code
- old direct message-to-points behavior
- old season mass reset

When porting, write new tests in the new project rather than assuming old behavior remains valid.

---

## 28. What to do when the contract is incomplete

Do not stop development for a missing tunable number.

If the mechanism is clear:

1. define a config field,
2. document it,
3. use a clearly marked development/test default if necessary,
4. continue implementation.

Stop and ask only when proceeding would require choosing between materially different product behaviors that cannot be safely isolated behind config/interface.

Examples that do **not** require stopping:

- whether Treasury rate is 8% or 10%,
- whether cooldown is 24h or 48h,
- exact Typing WPM formula coefficients.

Examples that would require contract clarification if not already covered:

- adding a new user role,
- adding a new permanent currency,
- changing whether War is Season 1,
- allowing multi-Clan simultaneous membership,
- making confirmed off-chain DLX allocation claimable, transferable, tradable, or on-chain during Season 1.

---

## 29. Agent completion checklist

Before saying a task is complete, the agent must verify:

- [ ] I read `DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md`.
- [ ] My changes stay inside the assigned phase/slice.
- [ ] I did not introduce Season 2 War/Defence logic.
- [ ] I did not invent or hardcode non-final economy values.
- [ ] New tunable values are config-driven.
- [ ] Critical mutations are idempotent.
- [ ] Database invariants/constraints are appropriate.
- [ ] Permission/auth negative cases are tested.
- [ ] Concurrency tests exist where required.
- [ ] Migrations are valid and coordinated.
- [ ] Redis state is not the only economic truth.
- [ ] Async retries cannot duplicate rewards.
- [ ] I added/updated tests.
- [ ] I ran the relevant local checks.
- [ ] I documented API/config/migration changes in the PR description.
- [ ] I called out any contract conflict instead of silently resolving it.
- [ ] This phase has a dedicated PR with appropriate GitHub Actions coverage.
- [ ] Every required CI check is green; no required check is failing, cancelled, missing or skipped.
- [ ] I stopped after the green PR and did not begin the next phase without explicit repository owner authorization.
- [ ] I did not merge the PR unless the repository owner explicitly asked me to.

---

## 30. Final rule

Build **the approved Season 1 product**, not the product an agent thinks would be better.

Prefer small, correct, testable, configurable increments.

Do not delay foundational development because balance numbers are still tunable, and do not use that flexibility as permission to invent game rules.
