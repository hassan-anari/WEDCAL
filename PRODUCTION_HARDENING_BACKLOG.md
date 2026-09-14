# Dexo Land Backend — Production Hardening Backlog

**Document role:** living production-readiness risk register
**Repository:** `hassan-anari/Dalexio-Game-Backend`
**Initial scope reviewed:** Phases 0–5 + Season 1 Economy Contract Sync + Base Tap
**Verified against Phase 6 PR:** `33e14c0b5178d1b79e2c3b5668ff257b012d23f1` (correction base)
**Phase 7A review base:** `132bcc187512bf38cf34e0e123f79c3b01493e6e`
**Phase 8 review base:** PR #12 final correctness pass (2026-09-12)
**Phase 9 correction review base:** PR #13 `5233ed302f8ae515a91a2c9aa9682ebbec9ffb6f`
**Phase 9 merged baseline:** `4ca42b1c2be8bc815648f031a7bd7a8b1c226a36`
**Phase 10 reviewed hosted HEAD:** `d049af43a3c9d18d4221e883413834d82125ae78`
**Initial review date:** 2026-09-10
**Last reviewed:** 2026-09-13 (Phase 10 PR #14 final correction; hosted baseline 503/505 tests)
**Owner:** repository/product owner

---

## 1. Purpose

This file records **real production risks that are intentionally not blocking feature development yet**, plus cross-phase requirements that become blockers when their trigger is reached.

The goal is to keep MVP development fast **without losing production rigor**.

This is **not** a product/economy Source of Truth and must never override:

1. `DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md`
2. `DEXO_LAND_SEASON1_ECONOMY_CONFIG_CONTRACT.md`
3. approved frontend/product behavior
4. `AGENTS.md`

If this backlog conflicts with a canonical contract, the canonical contract wins and this file must be updated.

No coding agent may use this file as permission to invent unresolved economy thresholds, formulas, unlock milestones, Season 2 rules, DLX chain precision, or product behavior.

---

## 2. Mandatory agent maintenance protocol

**Every future development phase must read this file before implementation and update it before the PR is considered complete.**

At the beginning of a phase, the coding agent must:

1. Read `AGENTS.md`, the canonical Source of Truth, the economy contract, and this file.
2. Identify entries whose **Target / Promotion Trigger** intersects the current phase.
3. Treat any entry promoted to a current P0/P1 correctness/security/economic-integrity issue as part of the current phase gate.
4. Do **not** implement unrelated tuning merely because it exists here.

Before the phase PR is handed back to the owner, the coding agent must:

1. Add any newly discovered deferred production risk that is real but not a current blocker.
2. Update entries materially changed by the phase.
3. Mark an entry `RESOLVED` only when code/config plus tests or operational evidence prove its acceptance criteria.
4. Update **Verified against commit** and **Last reviewed** at the top of this file.
5. Add a `Hardening Backlog Delta` section to the PR description containing:
   - IDs resolved,
   - IDs changed,
   - IDs added,
   - IDs promoted to a blocker,
   - or `No hardening-backlog changes` with a short reason.
6. Never delete a resolved item. Move it to the resolution log so the same historical issue is not repeatedly rediscovered.

### Required wording for future coding-agent prompts

Every future phase prompt should include this instruction:

> Read `PRODUCTION_HARDENING_BACKLOG.md` before implementation. Resolve any item whose promotion trigger is reached by this phase, without broadening scope into unrelated tuning. Before opening/updating the PR, update the backlog with newly discovered deferred risks, evidence for changed/resolved entries, the reviewed commit, and a `Hardening Backlog Delta` in the PR description. Do not mark an item resolved without tests or operational evidence. Do not invent product/economy rules.

---

## 3. Classification and promotion rules

### Status

- `OPEN` — known work/risk remains.
- `WATCH` — architecture is currently safe, but capacity/behavior must be measured before launch.
- `RESOLVED` — acceptance criteria have been verified and recorded in the resolution log.
- `SUPERSEDED` — replaced by an explicit newer contract/design; retain history.

### Gate

- `PHASE-BLOCKER` — may remain deferred today, but **must be resolved before the named target phase can be approved/merged** if that phase depends on it.
- `LAUNCH-BLOCKER` — may remain while reaching functional MVP, but **must be resolved before public production launch**.
- `MEASURE-BEFORE-LAUNCH` — benchmark/observe first; optimize only if the acceptance threshold is not met.
- `POLISH` — non-economic user/API/developer quality issue. It becomes blocking only if observed behavior is materially broken for the launch flow.

### Promotion rule

A deferred item is **not permanently P2**. It becomes a current P1/P0 when its trigger becomes true.

Examples:

- A 500-row worker limit is not automatically a current bug; it becomes a launch P1 if load tests show backlog cannot drain within its SLA.
- Missing durable redrive for an ACKed Telegram update is a correctness risk, not mere tuning, and must be fixed before features critically depend on that ingress path in production.
- An index mismatch is measured first; it becomes a blocker if realistic data produces unacceptable query latency or database pressure.

### Current development policy

Until functional MVP is complete:

- Fix correctness, security, race, idempotency, ledger, transaction, migration, and data-loss problems in the phase where they become real.
- Track capacity, query, infrastructure, and operational hardening here unless their trigger is already reached.
- Phase 11 is the final production-readiness gate, not optional cleanup.

---

# 4. Executive risk register

| ID | Area | State | Gate | Target / Promotion Trigger | Impact |
|---|---|---|---|---|---|
| PH-001 | Telegram inbound worker-loss redrive | RESOLVED | PHASE-BLOCKER | Resolved in Phase 7A before Clan membership consumes Telegram events | High |
| PH-002 | GameConfig publication bypass | VERIFYING | LAUNCH-BLOCKER | Before operators publish real economy configs | High |
| PH-003 | RewardLedger append-only enforcement | RESOLVED | PHASE-BLOCKER | PostgreSQL evidence accepted in Phase 9 | High |
| PH-004 | Mission DB definition ↔ config rule binding | VERIFYING | LAUNCH-BLOCKER | Before real production missions can be authored/enabled | High |
| PH-005 | Authoritative anti-Sybil / qualified-player eligibility | RESOLVED | PHASE-BLOCKER | Accepted in merged Phase 9 | High |
| PH-006 | Weekly epoch attribution + source-event watermark | RESOLVED | PHASE-BLOCKER | Accepted in merged Phase 9 | High |
| PH-007 | Weekly DLX settlement engine invariants | RESOLVED | PHASE-BLOCKER | Accepted in merged Phase 9 | High |
| PH-008 | Season-close drain/finalization of valid pending work | OPEN | PHASE-BLOCKER | Phase 11 | High |
| PH-009 | Moderation evidence retention/access audit | VERIFYING | PHASE-BLOCKER | Phase 7/10 when moderation/admin flows are implemented | High |
| PH-010 | Base Tap recovery throughput | WATCH | MEASURE-BEFORE-LAUNCH | Phase 11 load test; promote if backlog SLA fails | High at scale |
| PH-011 | Base Tap receipt cleanup/table growth | WATCH | MEASURE-BEFORE-LAUNCH | Phase 11 load test; promote if creation > cleanup sustainably | High at scale |
| PH-012 | Base Tap / Mini App ingress abuse throttling | OPEN | LAUNCH-BLOCKER | Before public traffic | High |
| PH-013 | Creator settlement scheduler capacity + poison retry policy | WATCH | MEASURE-BEFORE-LAUNCH | Phase 11; promote on lag/retry starvation | Medium/High |
| PH-014 | Referral reconciliation capacity + poison retry policy | WATCH | MEASURE-BEFORE-LAUNCH | Phase 11; promote on lag/starvation | Medium/High |
| PH-015 | Reward/referral outbox backlog observability + throughput | WATCH | LAUNCH-BLOCKER | Phase 11 operational gate | High |
| PH-016 | Global Referral attribution advisory lock | WATCH | MEASURE-BEFORE-LAUNCH | Viral/burst load test | Medium/High |
| PH-017 | Referral tier count query/index strategy | WATCH | MEASURE-BEFORE-LAUNCH | Large inviter/referral datasets | Medium |
| PH-018 | NetworkActivity hot-path query/index strategy | WATCH | MEASURE-BEFORE-LAUNCH | High Telegram message throughput | Medium/High |
| PH-019 | Creator re-verification vs Telegram Bot API limits | WATCH | LAUNCH-BLOCKER | Before large Creator program launch | Medium/High |
| PH-020 | Bootstrap typed contract + query budget | OPEN | POLISH / MEASURE | Phase 6/10 contract growth; Phase 11 latency gate | Medium |
| PH-021 | Reward history cursor/index alignment | WATCH | MEASURE-BEFORE-LAUNCH | Large reward histories | Medium |
| PH-022 | Typing policy flags accepted but not fully implemented | OPEN | LAUNCH-BLOCKER | Before publishing a config that changes those flags; final audit Phase 11 | Medium/High |
| PH-023 | Typing mission reset-period coupling | OPEN | LAUNCH-BLOCKER | Before Daily and Typing reset timezones may diverge | Medium |
| PH-024 | Typing expected failures → deterministic 4xx | OPEN | POLISH | Before frontend production integration is frozen | Medium |
| PH-025 | Daily streak display after a missed period | OPEN | POLISH | Phase 6/10 frontend/API polish | Low/Medium |
| PH-026 | Economy config cutover at unsafe mid-period boundaries | VERIFYING | LAUNCH-BLOCKER | Before live config operations | High operational |
| PH-027 | Settlement-control audit actor/reason | VERIFYING | LAUNCH-BLOCKER | Phase 10 admin operations | Medium/High |
| PH-028 | Django Admin static assets in deployed Uvicorn topology | VERIFYING | LAUNCH-BLOCKER | Before production Admin is relied upon | Medium |
| PH-029 | Production web/admin security hardening | OPEN | LAUNCH-BLOCKER | Phase 11 / production deployment | High |
| PH-030 | Secret/token rotation and log-redaction runbook | OPEN | LAUNCH-BLOCKER | Phase 11 | High |
| PH-031 | PostgreSQL backup + restore drill | OPEN | LAUNCH-BLOCKER | Phase 11 | Critical operational |
| PH-032 | Data lifecycle, purge batching, disk/WAL growth | WATCH | MEASURE-BEFORE-LAUNCH | Phase 11 with realistic datasets | High at scale |
| PH-033 | Celery delivery semantics / graceful shutdown / Beat singleton | OPEN | LAUNCH-BLOCKER | Phase 11; Telegram subpart promoted earlier by PH-001 | High |
| PH-034 | Metrics, alerting, SLOs and runbooks | OPEN | LAUNCH-BLOCKER | Phase 10/11 | High |
| PH-035 | DB connection pool / worker / broker sizing | WATCH | MEASURE-BEFORE-LAUNCH | Phase 11 | High at scale |
| PH-036 | Large-table migration safety policy | WATCH | LAUNCH-BLOCKER | Becomes mandatory once tables carry production-scale data | High |
| PH-045 | Unresolved Clan lifecycle product decisions | OPEN | PHASE-BLOCKER | Phase 7B dissolution/recovery design | Medium/High |
| PH-046 | Multi-Season Clan identity/state isolation | OPEN | SEASON-2-BLOCKER | Before a persistent Clan enters Season 2 | High |
| PH-047 | Accepted JOIN reservation and credential liveness | RESOLVED | CLOSED | Phase 8 correctness pass | High |
| PH-037 | Main-branch protection / ruleset enforcement | OPEN | LAUNCH-BLOCKER | Before multiple operators/agents or production release flow | Medium/High process |
| PH-038 | Contract-gate robustness beyond prose substring checks | OPEN | POLISH / GOVERNANCE | Before production governance freeze | Medium |
| PH-039 | Dependency/container vulnerability review | OPEN | LAUNCH-BLOCKER | Phase 11 | Medium/High |
| PH-040 | Season 2 REFERENCE/LIVE price source semantics | OPEN | PHASE-BLOCKER | Season 2 price integration, not S1 | High financial UI |
| PH-041 | DLX native precision / off-chain → chain conversion | OPEN | PHASE-BLOCKER | Before any on-chain claim/transfer | Critical financial |
| PH-042 | Fraud forfeiture/adjustment ledger + appeal/audit path | RESOLVED | PHASE-BLOCKER | Accepted in merged Phase 9; Phase-10 operations retained | High |
| PH-043 | `read-season-brief` authoritative INTERNAL evidence | OPEN | LAUNCH-BLOCKER | Does not block Phase 6/7 MVP development; final S1 mission/launch gate | High functional |
| PH-044 | Repeated same-key multi-season medal provenance | WATCH | SEASON-2-BLOCKER | Before Season 2 Profile/Collection integration | Medium product/API |

---

## PH-045 — Unresolved Clan lifecycle product decisions

**Origin:** Phase 7A Clan Core
**State:** `OPEN`
**Gate:** `PHASE-BLOCKER` before Phase 7B dissolution/recovery behavior

The contract does not yet decide Creator Group/Clan Home coexistence, repeated same-Season recreation after dissolution, or detailed Telegram authority-loss recovery triggers. Phase 7A does not invent these answers; Phase 7B requires owner-approved decisions and transition tests.

**Phase 8 context-ownership sub-decision (resolved):** a live `ACTIVE`/`RECOVERY`
ClanSeason exclusively owns its Telegram group as `CLAN_HOME`; registration rejects every
non-`INACTIVE` Creator registration and impossible dual-live data fails closed in message
routing. Ending a Clan does not reactivate historical Creator state. Other independent
PH-045 lifecycle questions remain open.

Phase 10 operational tooling must also provide an audited resolution action for Phase 7A
`MANUAL_REVIEW` invite rows carrying `unknown_credential_risk`; until resolved, the same
applicant cannot start another JOIN credential lifecycle for that ClanSeason/Home Base.

## PH-046 — Multi-Season Clan identity and state isolation

**Origin:** Phase 7A Clan Core
**State:** `OPEN`
**Gate:** `SEASON-2-BLOCKER`

Before Season 2, prove that one persistent `Clan` can receive a later `ClanSeason` without
recreating its identity and that stale Season 1 requests, invites, roles, and current-state
queries neither block nor authorize Season 2 membership. This is a future multi-Season
integration risk only; Phase 7A intentionally does not implement Season 2 behavior.

## PH-047 — Accepted JOIN reservation and credential liveness

**Origin:** Phase 7A final lifecycle review
**State:** `RESOLVED`
**Gate:** `CLOSED` by the Phase 8 correctness pass

**Phase 8 policy implemented:** an accepted path reserves capacity without a TTL. Applicant and
Captain cancellation both enter the durable revoke state machine; `UNCERTAIN`, unknown-risk,
`REVOKE_FAILED`, and `MANUAL_REVIEW` continue reserving capacity. The request changes to
`CANCELLED` only after a safe terminal revoke, with no leave counter/cooldown. No replacement JOIN
credential is issued while unresolved risk exists. The PostgreSQL suite now covers local-before-create cancellation, Captain/applicant paths,
unsafe-state reservation retention, global effective-capacity final-slot serialization,
create/revoke races, and concurrent Captain/applicant cancellation versus Telegram presence
observation. The final Phase 8 matrix also races positively confirmed and failed/unknown remote
revocation against presence activation. These races prove that activation and cancellation serialize, unsafe credentials
continue to reserve, safe terminalization releases exactly once, and neither path applies a
voluntary-leave count or cooldown. PH-047 is therefore resolved for the approved no-TTL policy.

## PH-048 — Clan `banner-raised` shared milestone predicate

**Origin:** Phase 8 Trophy Hall
**State:** `OPEN`
**Gate:** `PRODUCT-DECISION` before enabling `banner-raised`

The approved config names `CLAN_CREATED_AND_FIRST_SHARED_MILESTONE` but supplies no concrete,
server-owned milestone predicate. Phase 8 keeps evaluation fail-closed and awards no points;
product must define the event before this achievement can be enabled.

## PH-049 — Phase 8 economy projection reconciliation operations

**Origin:** Phase 8 Clan economy
**State:** `WATCH`
**Gate:** `LAUNCH-BLOCKER`

Treasury, Contribution, HQ XP, and competitive-score sources are durable, while aggregate fields
serve reads. Phase 8 now includes and tests deterministic source-ledger projection rebuilding,
including Treasury earned/current separation. Before launch, add an operator-safe bounded command
and drift metric around that proven service; never repair drift by deleting ledger rows or
recomputing historical entries with a newer config.

# 5. Detailed findings from completed phases

## PH-001 — Durable Telegram inbound redrive after worker loss

**Origin:** Phase 2 Telegram Identity & Ingestion
**State:** `RESOLVED`
**Gate:** `PHASE-BLOCKER` before Telegram becomes critical to Clan correctness; `LAUNCH-BLOCKER` regardless
**Evidence on current main:**

- `apps/telegram/api.py` durably persists an inbound update and then dispatches a Celery task before returning `200`.
- `apps/telegram/tasks.py::process_telegram_update` has exception autoretry, but no explicit late-ack / worker-lost policy.
- `TelegramInboundUpdate` has durable `PENDING/FAILED/...` state, but there is no periodic stale `PENDING/FAILED` redrive task in the current Beat schedule.
- A duplicate webhook request can dispatch again, but Telegram is not guaranteed to resend after it already received `200`.

**Failure mode:** worker process dies after broker delivery/early acknowledgement and before DB processing commits. The webhook has already been acknowledged, so an accepted update can remain stranded.

**Required acceptance:**

- Choose and test one durable strategy: late acknowledgement with worker-loss rejection **and/or** DB-backed stale-inbound redrive.
- Processing stays idempotent by `update_id`.
- Kill a worker after delivery but before commit and prove eventual processing.
- Poison events must move through explicit retry/dead-letter semantics rather than hot-looping forever.
- Track oldest pending age and failed count.

**Target:** resolve before Phase 7 is approved if Clan membership/activity depends on these events.

**Resolution evidence (Phase 7A):**

- Durable claims commit the incremented processing attempt before event effects begin; failure
  reconciliation is token-checked and row-locked. Rows also retain processing leases, retry
  schedules, error categories, and terminal `DEAD_LETTER` state.
- Beat runs a bounded PostgreSQL redrive using `SELECT ... FOR UPDATE SKIP LOCKED`; per-row dispatch failure cannot starve the batch.
- Tests cover stale `PENDING`, retryable `FAILED`, abandoned `PROCESSING`, non-starvation, and terminal poison behavior.
- Both exception and abandoned-worker paths enforce the same durable attempt ceiling; stale
  `PROCESSING` rows at the ceiling become `DEAD_LETTER` without redispatch.
- Expired `DEAD_LETTER` payload content is purged while update ID, terminal state, attempts, and
  error metadata remain durable; retryable payloads are retained.
- Telegram `update_id` and PostgreSQL remain correctness truth; Redis is not involved.

---

## PH-002 — GameConfig publication can bypass the publication service

**Origin:** Phase 3 Config
**State:** `VERIFYING` — Phase-10 PR #14 correction requires final hosted green evidence
**Gate:** `LAUNCH-BLOCKER`
**Evidence:** `apps/game_config/admin.py` makes published objects read-only, but a DRAFT object's `status` is still editable. `GameConfigVersion.save()` protects rows that were already published, but does not itself enforce that the DRAFT→PUBLISHED transition went through `publish_game_config`; queryset/update/maintenance paths can also bypass model save.

**Risk:** malformed or unvalidated economy configuration can become effective without checksum/publication chronology/semantic validation.

**Required acceptance:**

- Make `status` service-controlled in Admin.
- Enforce transition invariants outside presentation-only Admin controls; direct ORM/maintenance misuse must not silently publish invalid config.
- Test DRAFT→PUBLISHED bypass attempts.
- Preserve immutable published configs and effective-dated history.

**Target:** Phase 10 admin hardening at latest; earlier if production configs are operated before then.

---

## PH-003 — RewardLedger is not protected as append-only at the durable boundary

**Origin:** Phase 3 Rewards
**State:** `RESOLVED`
**Gate:** `PHASE-BLOCKER` before Phase 9
**Evidence:** Django Admin is read-only, but `RewardLedger` remains a normal Django model; application/maintenance ORM update/delete can mutate accounting history. Rebuild logic derives projections from this ledger.

**Risk:** silent mutation/deletion changes XP, rankings and later DLX allocation inputs while removing audit history.

**Required acceptance:**

- Enforce append-only semantics at a boundary stronger than Admin UI alone: database privilege/trigger pattern, explicit immutable repository/service pattern with tested DB guard, or equivalently strong mechanism appropriate to deployment.
- Corrections must use explicit adjustment/forfeiture entries, never historical rewrite.
- Tests prove update/delete of canonical ledger rows cannot succeed through normal application paths.
- Reconciliation still rebuilds projections from immutable history.

**Target:** must be resolved before Phase 9 consumes RewardLedger history for confirmed DLX allocation.

**Resolution evidence (Phase 9, reviewed base `bfe663b`):** PostgreSQL trigger migration
`rewards.0005` rejects every row-level UPDATE/DELETE, including direct SQL. Phase-9 PostgreSQL
tests prove inserts remain valid and both mutation forms fail; projection rebuild remains read-only.

---

## PH-004 — MissionDefinition can diverge from configured verification policy

**Origin:** Phase 4 Missions
**State:** `VERIFYING` — Phase-10 PR #14 correction requires final hosted green evidence
**Gate:** `LAUNCH-BLOCKER`
**Evidence:** `apps/missions/services.py` checks that a mission key exists in the effective config, but execution branches on the DB row's `verification_family` / `repeat_policy`. There is no canonical equality/compatibility check between the DB definition and the configured `MissionRule` before execution.

**Risk:** an operator could accidentally make a Telegram-membership mission behave like a timed external mission, changing eligibility/reward behavior without an approved economy config change.

**Required acceptance:**

- Bind enabled DB definitions to the effective rule's verification/repeat semantics, or make one side authoritative and reject mismatches.
- Validate at publication/enable time where possible, not only after a user starts the mission.
- Regression: a configured TELEGRAM mission cannot be rewarded through a TIMED_EXTERNAL DB definition.

**Target:** before production mission authoring/enabling.

---

## PH-005 — Anti-Sybil / qualified-player decision is not yet an authoritative subsystem

**Origin:** Phase 5 Referral + new DLX growth contract
**State:** `RESOLVED` — accepted Phase-9 hosted CI on merged HEAD
**Gate:** `PHASE-BLOCKER` before Phase 9
**Evidence:** the canonical contract says anti-fraud is a real S1 subsystem and referral/growth eligibility must not be raw account creation. `ReferralConfig` still exposes `anti_sybil_pass_required`, but current `referral_activation_is_safe()` evaluates progression, account age and moderation; it does not consume a durable anti-Sybil decision. The growth-unlock config has a `qualified_player_predicate`, but no final evaluator/snapshot engine exists yet.

**Risk:** referral and especially confirmed DLX/growth-unlock economics can be farmed by structurally valid but coordinated fake accounts if "qualified" remains only a loose proxy.

**Required acceptance:**

- Build an auditable eligibility decision/interface before confirmed DLX settlement.
- Store decision reason/evidence version and evaluation time; do not rely on a magic mutable boolean with no provenance.
- Manual moderation override, automated flags and re-evaluation semantics must be explicit.
- Growth milestones must count the same canonical qualified population snapshot used by the economic engine.
- Do not invent numeric thresholds in code; use approved config.

**Target:** Phase 9 blocker. Phase 7 moderation can provide primitives used here.

**Resolution evidence (Phase 9, reviewed base `bfe663b`):** `QualificationDecision` is an
append-only, policy-versioned decision history with deterministic current lookup and unique source
identity. Referral activation and growth counting use this authority and fail closed; tests cover
missing/versioned decisions, moderation exclusion and duplicate source identity.

**Correction evidence (hosted PR #13 HEAD `9e025b9`):** effective latest-decision queries now exclude a later
DISQUALIFIED/PENDING decision, use audited source-time moderation state, freeze the exact decision
row into immutable epoch snapshots, and reject qualification history UPDATE/DELETE in PostgreSQL.

---

## PH-006 — Weekly epoch attribution and queue watermark

**Origin:** Economy Sync + Base Tap, cross-phase
**State:** `RESOLVED` — accepted Phase-9 hosted CI on merged HEAD
**Gate:** `PHASE-BLOCKER` in Phase 9

**Why this matters:** weekly DLX distribution is based on the user's performance **inside each specific epoch**. A settlement must not accidentally classify work by processing time.

**Specific Base Tap risk:** Base Tap intentionally creates one canonical XP settlement per player/UTC day, and its RewardLedger provenance uses the day's first accepted tap timestamp. If a Season/epoch boundary is not aligned with UTC midnight, one daily aggregate can contain taps on both sides of an epoch boundary. A Phase 9 implementation that blindly groups the daily RewardLedger row by `occurred_at` could assign all taps to the wrong week.

**Cross-queue risk:** Telegram events, Creator windows, Base Tap accumulators, reward outbox work, mission verification and other source processing can arrive late. Freezing an epoch before all valid pre-boundary source events have reached a durable scored/snapshotted state can undercount users.

**Required acceptance:**

- Define a canonical source-time/epoch mapping independent of worker processing time.
- Either align all economic boundaries with aggregation boundaries or preserve sufficient per-epoch durable aggregates to split correctly.
- Phase 9 must not depend on expiring Base Tap receipts for permanent economic truth.
- Add an ingestion/reward **watermark/drain gate** before epoch freeze: all accepted source work with source time `< epoch_end` is processed or explicitly accounted as pending before the epoch becomes immutable.
- Test events received/processed late around both sides of an epoch boundary.

**Resolution evidence (Phase 9, reviewed base `bfe663b`):** six durable source-time epochs and an
explicit PostgreSQL drain provider registry cover Telegram ingress, Network/Clan activity and
windows, reward/referral outbox, Base Tap accumulators, and Typing wallets. Freeze holds a locked
epoch row, persists provider watermark evidence, and cannot proceed while pre-cutoff work remains.

**Correction evidence (hosted PR #13 HEAD `9e025b9`):** providers are Season/window scoped, user-held Typing balance
no longer blocks, unfinished Typing attempts do block, DRAINING evidence commits independently,
and permanent source-time Base Tap facts survive receipt expiry and feed frozen raw snapshots.
The final correction also records completed Typing CREDIT performance as an append-only fact at the
attempt completion source time, independently of a later voluntary claim.
Typing-derived passive Referral performance is captured at the same boundary in a distinct
VERIFIED_REFERRAL fact with immutable relation, tier, config and source-time provenance. A later
Typing claim and Referral RewardLedger settlement are excluded from weekly aggregation for that
same source, so neither batching nor post-freeze processing can reinterpret or double-count it.

---

## PH-007 — Weekly DLX settlement engine invariants

**Origin:** Economy Sync; implementation intentionally assigned to Phase 9 after Clan inputs exist
**State:** `RESOLVED` — accepted Phase-9 hosted CI on merged HEAD
**Gate:** `PHASE-BLOCKER` in Phase 9

**Required architecture/acceptance:**

- Six exact S1 economic epochs; no seventh economic week.
- 60M base pool, max 100M, exact integer/off-chain-DLX amount accounting; no binary float truth.
- Operational `settlement_start_epoch` supports delayed catch-up in chronological order.
- Epoch state machine equivalent to `OPEN → FROZEN/PENDING → SCORING → REVIEW → SETTLED`.
- Once settled, history is immutable.
- Same-category unused budget carries forward deterministically.
- Growth unlock events are durable, idempotent and unique per milestone.
- A growth unlock affects only still-open eligible epochs and never rewrites frozen/settled budgets.
- `settlement_started_at` is set atomically with the first real settlement transition so operator changes cannot rewrite history.
- Separate append-only DLX allocation ledger; DLX allocation must never increase XP/Level or recursively create Referral/Clan/Creator rewards.
- Deterministic rounding/remainder reconciliation proves exact epoch/season totals.
- Anti-fraud review and explicit post-settlement forfeiture/adjustment path; never edit historical allocation in place.
- Delayed Week 1/2 settlement still scores Week 1/2 performance windows independently.
- Failure midway through a large settlement must be safely retryable without partial visible economic history.

**Do not invent** scoring formulas or milestone thresholds that the owner has not approved.

**Resolution evidence (Phase 9, reviewed base `bfe663b`):** durable epoch, frozen snapshot,
category budget, batch and allocation models enforce chronological atomic settlement and exact
`effective = allocations + carry-out + residual` reconciliation. Production scoring and growth
distribution deliberately return typed fail-closed results because canonical formulas are absent.

**Correction evidence (hosted PR #13 HEAD `9e025b9`):** freeze creates the five-category raw snapshot population,
PostgreSQL protects frozen/settled inputs, carry materializes only after the preceding epoch settles,
Season locking serializes the operational control, and reconciliation aggregates actual allocation
ledger rows. Delayed carry, drift, immutable-history and PostgreSQL contention tests are green.

---

## PH-008 — Season-close drain and finalization barrier

**Origin:** Phase 4/5 + Economy Sync
**State:** `OPEN`
**Gate:** `PHASE-BLOCKER` in Phase 11

Current central reward guard correctly prevents new S1 economic events during days 43–45, but valid work from before the Day-42 cutoff can still be pending asynchronously.

Examples requiring explicit close handling:

- Telegram inbound updates accepted before cutoff but not processed yet.
- Creator windows not yet settled.
- Reward settlement outbox/referral-share work still pending.
- Base Tap accumulators with valid pre-cutoff accepted XP.
- mission rows already `REWARD_READY` but not claimed.
- Typing wallet credits earned before cutoff but not yet claimed.

**Important:** current Typing config validates options such as `auto_settle_at_season_close` / source-Season preservation, but these policy flags are not all represented in the runtime typed config or close workflow yet.

**Required acceptance:**

- Freeze new earning at Day 42 while continuing reconciliation of valid pre-cutoff source events.
- Drain or watermark all economic queues before final S1 snapshots.
- Auto-settle/finalize every contractually claimable pre-cutoff balance that must survive close, preserving source Season/config.
- No valid reward is lost because its worker ran after Season status changed.
- No post-cutoff activity becomes eligible due to late processing.
- Rebuild/reconcile XP projections before rankings/DLX finalization.
- Run a kill/restart close drill and verify exact final totals.

---

## PH-009 — Moderation evidence access and retention audit

**Origin:** Phase 5 evidence primitives; Phase 7B retention holds; staff tooling continues in Phase 10
**State:** `VERIFYING` — audited Admin viewer implemented; final hosted green evidence required
**Gate:** `PHASE-10-BLOCKER` before staff evidence access is exposed

Phase 7B adds bounded evidence references/holds: active cases protect referenced NetworkActivity
raw content and fingerprint/risk metadata, case close records a config-snapshotted finite retention
deadline, and normal player APIs never return raw evidence. Phase 10 still owns the least-privilege
staff evidence viewer and read-access audit trail; no staff evidence endpoint is exposed in Phase 7B.

**Required acceptance:**

- [x] Evidence attached to an active moderation case is not purged by normal bounded cleanup.
- [x] Raw content is not exposed in normal player APIs.
- Admin/support read access is least-privilege and audited.
- [x] Purge resumes after the finite, config-derived retention hold expires.
- [x] Retention is versioned policy rather than indefinite raw-content storage.

---

# 6. Scale and throughput risks — measure, then tune

## PH-010 — Base Tap recovery throughput

**Current design:** one durable accumulator per player/Season/UTC day; recovery scans up to 500 candidates per Beat run and processes them sequentially. Beat currently runs every 60 seconds.

**Why it is not a current merge blocker:** accepted economic input is durable and recovery is retry/lease-safe.

**Why it can become a launch blocker:** at very high daily tapper counts, settlement backlog can take too long to drain and collide with weekly/final settlement deadlines.

**Acceptance before launch:**

- Load test at 50K / 100K / 250K / 500K / 1M synthetic daily users.
- Define an oldest-pending settlement SLA tied to weekly cutoff requirements.
- If SLA fails, use bounded fan-out, `SKIP LOCKED`, sharded/ranged workers, bulk scheduling or another measured solution.
- Prove duplicate settlement remains impossible under worker concurrency.

---

## PH-011 — Base Tap receipt cleanup and table growth

Accepted client batches create short-lived idempotency receipts. The daily economic write is bounded, but receipt count is bounded by accepted batch fragmentation, not by one row/day. Cleanup currently deletes a bounded batch per periodic task.

**Worst-case planning concern:** many users can submit small accepted batches, creating substantially more receipts than daily accumulators. A cleanup rate below sustained creation rate grows indexes/table/WAL even though rows are eventually eligible for deletion.

**Acceptance:**

- Measure receipts/user/day under the real frontend batching strategy and adversarial-but-valid fragmentation.
- Measure create rate vs purge rate and DB bloat.
- Tune cleanup batch/frequency or use partition/TTL-style lifecycle only if data shows need.
- Ensure cleanup never removes provenance required by Phase 9 weekly scoring.

---

## PH-012 — Base Tap / Mini App request abuse throttling

Base Tap's economic cap is durable, but a rate-limited request can still authenticate, resolve a Player, acquire/read DB state and return zero. Economic safety is not the same as ingress-cost safety.

**Acceptance:**

- Add reverse-proxy and/or application-level per-user/IP/bot-aware request throttling for expensive mutation surfaces.
- Keep the DB daily cap as the economic truth; never rely on Redis throttling for reward correctness.
- Return stable 429/4xx behavior, not 500.
- Test abuse bursts without exhausting DB connections/worker capacity.

---

## PH-013 — Creator settlement scheduler capacity / poison retry

Current scheduler is bounded and the previously found stale-row starvation bug is fixed. However, repeated unexpected failure of many old windows has no durable per-window backoff/dead-letter model; unprocessed rows remain candidates.

**Acceptance:**

- Measure windows/minute and oldest unprocessed-window age under realistic message volume.
- Alert on oldest pending age.
- If poison windows can dominate the bounded scan, add durable retry/backoff/error state or a dedicated window queue model.
- Preserve advisory-lock and idempotent settlement semantics.

---

## PH-014 — Referral reconciliation capacity / poison retry

`reconcile_pending_referrals` currently processes a bounded oldest set and isolates exceptions, which protects a single batch. Unexpected failing relations are logged but do not receive a dedicated generic-error backoff in the task itself.

**Acceptance:**

- Measure pending relation lag under viral onboarding.
- Ensure many poison relations cannot permanently occupy the first page and starve healthy relations.
- Add retry scheduling/error state if observed/credible at launch scale.
- Alert on oldest `next_evaluation_at` lag.

---

## PH-015 — Reward outbox throughput and backlog observability

The reward/referral outbox now has processing state, lease and stale-processing recovery. Correctness is substantially improved, but launch requires capacity evidence.

**Acceptance:**

- Measure events/sec and queue drain after a burst.
- Metrics: PENDING count, PROCESSING count, oldest eligible age, retry count, deferred count, error categories.
- Alert before backlog age approaches epoch/Season close boundaries.
- Failure drill: kill workers mid-outbox processing and prove eventual exactly-once economic outcome.

---

## PH-016 — Global Referral attribution advisory lock

`attribute_start()` currently uses one global PostgreSQL advisory transaction lock to serialize cycle detection. This is correctness-safe but serializes every referral attribution globally.

**Risk trigger:** viral acquisition burst where referral starts become a significant write path.

**Acceptance:**

- Benchmark referral attribution throughput and lock-wait time.
- Keep current implementation if it meets burst SLO.
- Otherwise replace with a correctness-preserving narrower serialization/graph strategy; never trade cycle safety for throughput.

---

## PH-017 — Referral tier count query at large inviter scale

Tier calculation counts verified referrals for the inviter with a timestamp condition. FK indexing helps, but there is no dedicated `(inviter, status, verified_at)` composite index/projection in the current model.

**Acceptance:** use realistic high-fanout inviters with `EXPLAIN (ANALYZE, BUFFERS)`; add index/counter projection only if query plans/latency justify it. Reconciliation must prove any projection matches durable relations.

---

## PH-018 — NetworkActivity hot-path queries/indexes

Per message, duplicate/rate qualification performs sender/time and fingerprint/time queries. Current indexes include sender/time but not composite sender+fingerprint+time indexes for both duplicate paths.

**Acceptance:**

- Benchmark Telegram message ingest at expected and breakout rates.
- Inspect query plans and DB CPU/IO.
- Add composite/partial indexes or short-lived cache accelerators only if measured; PostgreSQL remains economic truth.
- Confirm index cost does not make ingestion writes worse than the reads it saves.

---

## PH-019 — Creator re-verification vs Telegram API limits

Creator re-verification can enqueue many registrations and performs external `getChatMember` calls for bot/creator membership. The client has a finite timeout and recognizes 429/retry-after, but program-wide rate coordination is not yet a production control plane.

**Acceptance:**

- Global/per-bot Telegram API request budget.
- Honor `retry_after` and add jittered retry scheduling.
- Avoid thundering herd after worker/restart outages.
- Metrics for 429, timeout, verification backlog age and `ADMIN_REQUIRED` transitions.

---

## PH-020 — Bootstrap typed API contract and query budget

Current `/bootstrap` returns `dict[str, Any]`. As Profile, Clan, Arena and notifications are added, this endpoint can become both an OpenAPI drift risk and a query fan-out hotspot.

**Acceptance:**

- Introduce explicit nested response schemas before frontend/backend contract freeze.
- Add schema/contract tests for required fields and optional states.
- Establish a query-count and latency budget with a realistic player (missions, referrals, clan, collection, notifications).
- Avoid growing bootstrap into an unbounded N+1 aggregator.

---

## PH-043 — Season Brief INTERNAL mission lacks authoritative completion evidence

The enabled Season 1 `read-season-brief` mission has no approved server-owned fact or verifier.
The existing INTERNAL verifier therefore fails closed rather than accepting a client assertion.

**Status:** OPEN
**Gate:** LAUNCH-BLOCKER
**Target / Promotion Trigger:** This does not block Phase 6/7 MVP feature development today, but
must be resolved before the final Season 1 mission/launch gate, once the authoritative
server-observed completion action is approved.

**Acceptance:** Define the approved server-owned read/open evidence, implement an authenticated
idempotent recording action, connect it to normal Mission verification, and cover authorization and
retry behavior without awarding points directly from the evidence endpoint.

---

## PH-044 — Repeated same-key multi-season medal provenance presentation

`PlayerMedalAward` can retain a source Season, but current Phase 6 reconciliation and Profile API
collapse owned awards by `medal_key`. This is safe while no repeat Season medal is finalized in S1.
Before Season 2 Profile/Collection integration, product must define whether repeated same-key
Season awards expose the first, latest, or complete award provenance.

**Status:** WATCH
**Gate:** SEASON-2-BLOCKER
**Target / Promotion Trigger:** Before Season 2 Profile/Collection contract and API integration.

**Acceptance:** Approve first/latest/all presentation semantics, update the typed API and
reconciliation projection accordingly, and add multi-season award ordering/provenance tests. Do not
collapse ambiguous repeated awards silently.

---

## PH-021 — Reward history cursor/index alignment

The history endpoint filters by player and orders/paginates by descending `id`, while the current composite history index is `(player, -occurred_at, -id)`.

**Acceptance:** benchmark large per-player history. If PostgreSQL sorts/scans excessively, either add `(player, -id)` or paginate by the indexed `(occurred_at,id)` tuple with stable cursor semantics.

---

# 7. Config/API fidelity and product-state polish

## PH-022 — Typing configuration flags are validated but not all are runtime policy

Current parser validates flags including:

- `completed_invalid_attempt_consumes_quota`
- `technical_failure_consumes_quota`
- `claimable_wallet_enabled`
- `claim_anytime`
- `auto_settle_at_season_close`
- `claim_preserves_source_season`

but the typed `TypingConfig` and services do not represent/honor all of them. Current behavior effectively assumes specific values.

**Risk:** future operator changes config believing behavior is supported, while runtime silently keeps old semantics.

**Acceptance:** for each accepted flag, either implement it and test both values, or reject unsupported values at config publication until support exists. Never accept configuration that runtime silently ignores.

---

## PH-023 — Typing mission reset timezone coupling

Typing attempts use `typing.reset_timezone`; mission eligibility currently derives the daily mission period from `daily.reset_timezone`. The internal `typing-session` verifier compares those period keys.

**Risk trigger:** Daily and Typing timezones/reset rules diverge.

**Acceptance:** derive `typing-session` from the Typing period, or explicitly validate that the two reset policies must match while this verifier relies on shared keys. Test a divergent timezone case.

---

## PH-024 — Expected Typing failures should not surface as 500s

`POST /typing/attempts/start` can raise expected quota/unavailable/idempotency errors without deterministic route-level mapping; submit already handles expected validation errors more explicitly.

**Acceptance:** define stable 4xx/409/429-style API semantics for normal user states and include typed error responses in OpenAPI/contract tests.

---

## PH-025 — Daily streak display after a missed period

`get_daily_state()` correctly computes the next reward as a fresh streak after a missed period, but currently reports the previous historical streak until the user claims again.

**Acceptance:** return effective current streak/state consistently with the next cycle reward. Add bootstrap regression for a missed full period.

---

## PH-026 — Economy config changes at unsafe time boundaries

**State:** `VERIFYING` — domain-specific preview/rejection implemented; final hosted green evidence required

Immutable/effective-dated config history is good, but a new config can become effective in the middle of a daily/Typing/Base Tap period or Creator window. Base Tap currently refuses further taps for the same UTC-day accumulator when its config version differs, which is safe but operationally surprising.

**Acceptance:**

- Define an operator runbook for economy config effective times.
- Prefer reset/epoch-aligned cutovers unless a feature explicitly supports split-period versions.
- Publication/preview should warn or reject unsafe effective times for sensitive fields where appropriate.
- Test in-flight Daily, Typing, Mission, Creator and Base Tap state across cutovers.

---

## PH-027 — Settlement control changes need actor/reason audit

**State:** `VERIFYING` — controlled Admin operation and DB guard require final hosted race evidence

`SeasonSettlementControlEvent` records previous/new epoch and timestamp, but not the operator identity or reason. Once this becomes a financially meaningful control, provenance matters.

**Acceptance:** Phase 10 admin action/service records authenticated actor (or service actor), reason/ticket/reference and request correlation where applicable. No direct silent DB edit path.

---

# 8. Infrastructure, security and operations

## PH-028 — Django Admin static assets

**State:** `VERIFYING` — production Docker Admin login/static smoke added; hosted result required

Historical Phase 1 review identified that the Uvicorn image has no explicit `collectstatic`/WhiteNoise/reverse-proxy static path. Current Dockerfile still contains no static collection/serving step and settings expose only `STATIC_URL`.

**Acceptance:** choose the production topology (reverse proxy/object storage/WhiteNoise), run `collectstatic` appropriately, and smoke-test `/admin/` with CSS/JS assets before Admin is an operational dependency.

---

## PH-029 — Production web/admin security hardening

Current production settings correctly require important secrets, disable DEBUG, set secure cookies and proxy SSL header. Final production hardening still needs an environment-specific review.

**Acceptance checklist:**

- HTTPS redirect/HSTS only when deployment proxy topology is verified.
- Trusted proxy and host configuration cannot be spoofed.
- Correct CSRF trusted origins for Admin if used.
- Admin not broadly exposed to the public Internet without compensating controls; least privilege, strong auth and preferably network restriction/SSO/2FA where platform supports it.
- Secure cookie/SameSite behavior tested with Telegram Mini App flows.
- Request/body-size limits at proxy/application edge for webhook and mutation endpoints.
- Security headers verified from the deployed service, not only settings files.

---

## PH-030 — Secret rotation and sensitive logging runbook

Secrets include Telegram bot token, webhook secret, Django secret, DB credentials and broker/cache credentials. Telegram raw payloads/message content are sensitive data.

**Acceptance:**

- Secrets come from deployment secret management, not committed files.
- Rotation procedure exists for bot/webhook/DB/broker secrets without unrecoverable downtime.
- Logs never contain full initData, bot tokens, webhook secrets, raw authorization headers or unnecessary message content.
- Rotation is tested in staging.

---

## PH-031 — PostgreSQL backup and restore drill

PostgreSQL is the durable source of truth for identity and economic history. A backup policy without a tested restore is insufficient.

**Acceptance:**

- Automated encrypted backups with explicit retention/RPO/RTO.
- Point-in-time recovery if supported by deployment target.
- Restore into isolated staging and run application/reconciliation checks.
- Verify RewardLedger, future DLX allocation ledger, Season state, referrals, Clan economy and moderation evidence consistency after restore.
- Document who can trigger restore and how DNS/app traffic is handled.

---

## PH-032 — Data lifecycle, purge batching, disk/WAL/autovacuum

Known high-growth tables include Telegram inbound audit rows, NetworkActivity, RewardLedger/outbox, Base Tap accumulators/receipts, and future Clan/activity/allocation ledgers. Some purge operations currently use broad SQL updates; Base Tap purge is bounded.

**Acceptance:**

- Estimate row/day and retained bytes for expected/stretch/breakout scale.
- Monitor DB size, index size, WAL, dead tuples and autovacuum lag.
- Purges use bounded/keyset batches when broad transactions become expensive.
- Partition/archive only when measurements justify it; do not prematurely partition every table.
- Failed/poison Telegram rows must eventually enter an explicit dead-letter/audit lifecycle so sensitive payloads do not remain forever accidentally.

---

## PH-033 — Celery delivery semantics, graceful shutdown and Beat singleton

Different task families have different economic/reliability needs. Current code has several application-level leases/idempotency protections, but final worker/broker behavior must be deliberate.

**Acceptance:**

- Explicit ack/worker-lost policy for tasks whose DB work must survive worker termination.
- Graceful worker shutdown duration covers in-flight critical transactions or redrive guarantees them.
- Worker prefetch/concurrency measured for long vs short tasks.
- Exactly one Beat scheduler (or a leader/lock) in production; duplicate Beats must not multiply operational load.
- RabbitMQ durable queue/message settings verified.
- Restart/broker-disconnect drill demonstrates eventual processing with no duplicate economic effects.

PH-001 is the early-phase Telegram-specific subset of this broader item.

---

## PH-034 — Metrics, alerting, SLOs and runbooks

Structured logs exist, but production launch needs actionable metrics and alert thresholds.

**Minimum launch metrics:**

- HTTP rate/latency/error by endpoint.
- DB pool usage, lock waits, slow queries, transaction latency.
- Telegram webhook accepted/rejected/deduped; oldest pending inbound age; failed count.
- Celery/Rabbit queue depth/age and worker availability.
- Reward outbox oldest age/retries/deferred.
- Base Tap pending/retry oldest age and settlements/minute.
- Creator unprocessed-window oldest age, settlement failures, Telegram API 429/timeouts.
- Referral pending oldest age and reconciliation failures.
- Reward counts by source plus anomaly rates.
- Phase 9 DLX epoch state, allocation totals, residual, fraud-held/forfeited amounts.
- Redis memory/eviction/restart indicators.
- disk/WAL/backup freshness.

**Acceptance:** every alert has a short operator runbook: what it means, first checks, safe mitigation, escalation.

---

## PH-035 — Database pool / worker / broker sizing

Current DB pooling is optional and worker counts are development defaults. Do not guess production sizing early.

**Acceptance:** use Phase 11 load profiles to set API workers, Celery queues/concurrency, DB max connections/pool sizes, broker memory/disk limits and Redis maxmemory policy. Ensure worst-case workers cannot exceed DB connection budget.

---

## PH-036 — Large-table migration safety

All current migrations are still early-project migrations. Once production/high-volume data exists, a syntactically valid migration can still lock a large table for too long.

**Acceptance for later phases/data:**

- Review generated SQL and lock behavior for indexes/constraints/defaults/backfills.
- Use concurrent indexes or staged nullable/backfill/constraint validation patterns where necessary.
- Avoid one-transaction massive data migrations.
- Define rollback/forward-fix plan.
- Test migration against a production-sized synthetic DB before release.

This is an **ongoing agent requirement** once table volume becomes material.

---

## PH-037 — Main branch protection / repository ruleset

At the initial review point, GitHub reports `main` as unprotected even though `AGENTS.md` defines a strict PR/green-check/owner-approval gate.

**Risk:** process is currently enforced by discipline, not repository controls; accidental direct push or merge can bypass CI/review.

**Acceptance:** configure branch protection/ruleset appropriate to the account/repository capabilities: require PR, required Contract/Foundation checks, block force pushes/deletion, and preserve owner emergency procedure without making routine bypass easy.

---

## PH-038 — Contract gate robustness

`validate_contract.py` now checks significantly more semantic markers than the original Phase 0 version, including the S1 45-day/6-epoch/Base Tap/DLX rules. Some Source-of-Truth gate checks remain prose-substring based.

**Acceptance:** before governance freeze, add tests for the validator itself or move critical machine-verifiable contract values into structured configuration/schema checks so negated/reworded prose cannot accidentally satisfy a critical invariant.

Do not turn this into a documentation parser project unless a real gap is demonstrated.

---

## PH-039 — Dependency and container vulnerability review

Current CI covers lint/type/tests/migrations/Docker smoke. Final release should also know whether runtime dependencies/base images carry known critical vulnerabilities.

**Acceptance:** dependency/container scan in release CI or documented release process; block only on actionable critical/high findings according to an owner-approved policy. Pin/update deliberately and rerun full regression tests.

---

# 9. Season 2 / chain-bound future gates already visible now

## PH-040 — REFERENCE vs LIVE DLX price semantics

Season 1 must expose no fake market price. Season 2 may show a reference/indicative value before market open and LIVE only after a real market exists.

**Acceptance before price UI/API:**

- Explicit status enum: `UNAVAILABLE`, `REFERENCE`, `LIVE` (or equivalent).
- LIVE includes source and `as_of` timestamp plus stale-data behavior.
- Reference price is never labeled as real market price.
- Market feed outage/staleness does not silently reuse an old value as fresh.
- Financial display uses decimal-safe amount formatting.

---

## PH-041 — Native DLX precision and on-chain conversion

The S1 backend correctly uses an off-chain DLX allocation amount and intentionally does not pretend native smallest-unit precision is finalized.

**Acceptance before claim/transfer:**

- Canonical chain decimal precision formally approved.
- One deterministic conversion rule from off-chain allocation amounts to native units.
- Rounding/remainder policy documented and tested on the full allocation population.
- Sum of claimable/on-chain units reconciles to the authorized off-chain ledger and supply budget.
- No migration silently multiplies/divides existing allocations by an assumed precision.

---

## PH-042 — Fraud adjustment / forfeiture ledger

**State:** `RESOLVED` — accepted Phase-9 hosted CI on merged HEAD
**Gate:** `PHASE-BLOCKER` in Phase 9

Confirmed settlement history should not be edited in place when later fraud is proven.

**Acceptance:**

- Append-only allocation plus explicit adjustment/forfeiture records.
- Reason/evidence/case/operator/config provenance.
- Deterministic balance projection: confirmed, held, forfeited, vested, claimable.
- Repeat moderation action cannot double-forfeit.
- Appeal/reversal uses a new audited adjustment, not mutation of history.

**Target:** Phase 9 allocation primitives + Phase 10 moderation/admin + Phase 11 reconciliation.

**Resolution evidence (Phase 9, reviewed base `bfe663b`):** immutable confirmed allocations plus
append-only HOLD/RELEASE_HOLD/FORFEIT/REVERSAL records carry reason, evidence, actor, config and
idempotency provenance. Locked balance checks reject over-adjustment and deterministic read models
keep Season-1 claimable at zero.

**Correction evidence (hosted PR #13 HEAD `9e025b9`):** PostgreSQL rejects allocation/adjustment UPDATE and DELETE;
strict request identity compares actor/evidence/metadata; advisory-key plus allocation-row locking
makes duplicate FORFEIT and HOLD/FORFEIT contention converge; release and reversal append new rows.

---

# 10. Phase ownership / promotion matrix

This matrix is a routing guide, **not permission to expand a phase beyond its canonical scope**.

| Future phase | Backlog items that become relevant/blocking |
|---|---|
| **Phase 6 — Profile & Collection** | PH-020 if bootstrap/profile contract is expanded; PH-024/PH-025 may be fixed opportunistically only if shared API work is touched. Add new Profile/Collection query/privacy risks here. |
| **Phase 7 — Clan Core + Governance + Moderation + Recovery** | **PH-001 must be reconsidered as a blocker before Telegram membership drives Clan correctness.** PH-009 moderation evidence. Add all Clan membership race/governance/recovery risks discovered. |
| **Phase 8 — Clan Progression & Economy** | Add treasury/contribution/inventory ledger and concurrency risks. Do not defer double-spend/double-credit correctness to Phase 11. |
| **Phase 9 — Arena Rankings + Weekly DLX settlement** | **PH-003, PH-005, PH-006, PH-007, PH-042 are blockers.** Also measure PH-021 where ranking/history read paths overlap. |
| **Phase 10 — Notifications + Broadcast + Admin** | PH-002, PH-009, PH-027, PH-028, PH-034; build RBAC/audit/preview for dangerous admin actions. |
| **Phase 11 — Season Close & Production Hardening** | PH-008 and PH-043 plus all remaining `LAUNCH-BLOCKER` and every S1 `WATCH` item through realistic measurement. PH-029–PH-039 form the core production-readiness gate. |
| **Season 2 integration** | PH-040, PH-041, PH-044 plus new Exchange/War/Arena/Trading-specific risks added by their own approved contract. |

---

# 11. Mandatory Phase 11 production-readiness test plan

Phase 11 must not be considered complete just because unit/integration tests are green.

## 11.1 Scale tiers

Use synthetic datasets/load profiles at approximately:

- 50K registered/qualified users — baseline
- 100K — expected successful campaign
- 250K — stretch
- 500K — breakout
- 1M — stress scenario

Do not model all registered users as simultaneous HTTP connections. Test separate profiles for API burst RPS, Telegram ingress, queue drain, ranking rebuild, and weekly settlement dataset size.

## 11.2 Required drills

### A. Full weekly settlement dry run

- ingest realistic source activity
- allow async queues to lag
- drain to watermark
- freeze epoch
- score/snapshot
- fraud review
- allocate category pools
- carry forward unused budget
- reconcile deterministic remainder
- publish confirmed DLX
- rerun settlement and prove no duplicate allocation

### B. Delayed-settlement catch-up

With `settlement_start_epoch=3`:

- Weeks 1 and 2 close independently.
- Week 3 opens settlement gate.
- Epoch 1 then 2 settle chronologically from their own frozen windows.
- Growth unlocks do not retroactively rewrite frozen/settled budgets.

### C. Worker/process failure drill

Kill workers at critical points in:

- Telegram inbound processing
- reward/outbox processing
- Base Tap finalization
- Creator settlement
- referral reconciliation
- weekly DLX settlement

Restart and prove eventual completion with no lost/double economic event.

### D. Infrastructure restart drill

Restart Redis and RabbitMQ separately; restart API/worker/Beat; simulate temporary PostgreSQL unavailability. Confirm Redis loss does not lose economic truth and queue recovery behavior is documented.

### E. Season-close drill

At Day 42 boundary:

- new economic source events are rejected after cutoff;
- pre-cutoff delayed work can still reconcile;
- all pending wallets/ready claims/windows/outboxes are drained according to contract;
- rankings and XP projections reconcile;
- DLX totals exactly match authorized settled pools;
- close can be safely retried after interruption.

### F. Backup/restore drill

Restore a production-sized snapshot into staging and rerun reconciliation. Compare canonical ledger totals and key row counts before/after.

---

# 12. SLOs to define before public launch

Exact numbers should be chosen from real infrastructure/load testing, not invented here. At minimum define measurable limits for:

- API p95/p99 latency for bootstrap and mutation endpoints.
- Telegram webhook ACK latency.
- oldest unprocessed Telegram update age.
- oldest reward outbox item age.
- oldest Base Tap pending settlement age.
- oldest Creator window age.
- oldest referral reconciliation lag.
- weekly settlement completion deadline after epoch freeze.
- acceptable DB lock wait / slow-query threshold.
- backup RPO/RTO.
- error-rate and queue-depth alert thresholds.

Once approved, record the numeric SLOs here or in a linked operations contract.

---

# 13. Historical findings already resolved — do not reopen without new evidence

These items were found during Phases 0–5 / PR #7 reviews and are considered resolved at the initial reviewed commit. Future agents should not spend correction cycles re-litigating them unless new code regresses the invariant.

| Historical finding | Current resolution evidence |
|---|---|
| Runtime PostgreSQL driver/libpq issue | `psycopg[binary,pool]` is now a runtime dependency; current Docker build/runtime smoke passed in CI. |
| Telegram raw payload purge not scheduled | Beat schedule includes `purge-expired-telegram-payloads`. |
| Stale/out-of-order bot/user membership overwrite | membership/group metadata use Telegram `update_id` ordering fields before applying state. |
| Missing Creator settlement dispatch | periodic `settle-creator-windows` task exists. |
| Referral enabled switch ignored | current referral attribution/activation/share paths check effective referral enable state. |
| Reward outbox abandoned PROCESSING rows | lease + stale PROCESSING recovery is present. |
| Speaker reward switch had no implementation | `record_activity` implements config-gated `NETWORK_SPEAKER` reward path. |
| Exact duplicate window ignored | network qualification now checks both exact and normalized fingerprints with their configured windows. |
| Base Tap one-row-per-tap / unbounded rejected persistence | one daily accumulator; accepted-only bounded receipts; zero/rejected submissions create no receipt. |
| Base Tap concurrent daily-cap race | Player/accumulator locking and DB constraints; real-PostgreSQL concurrency regression added. |
| Base Tap historical recovery regressed newer progression config | historical ledger provenance is preserved while persistent progression uses current projection policy. |
| S1 42-day earning / 3-day close boundary missing | central economic-window guard added and representative paths covered. |
| Final Creator window could fail at Day-42 cutoff | deterministic in-window provenance fixed. |
| Referral activation could crash during closing window | closing-window activation now defers safely. |
| Creator scheduler stale non-rewardable starvation | exact-window rows are all marked processed after successful finalization; failed windows stay retryable. |

---

# 14. New-risk template

Future agents should add items using this template:

```markdown
## PH-XXX — Short title

**Origin:** Phase N / component
**State:** `OPEN | WATCH | RESOLVED | SUPERSEDED`
**Gate:** `PHASE-BLOCKER | LAUNCH-BLOCKER | MEASURE-BEFORE-LAUNCH | POLISH`
**Target / Promotion Trigger:** ...

**Evidence:**
- file/function/query/PR observation

**Failure mode / why it matters:**
...

**Do not over-engineer:**
...

**Acceptance criteria:**
- ...
- ...

**Resolution evidence:**
- commit/PR/tests/benchmark, only when resolved
```

Rules:

- Prefer a concrete code path/failure mode over generic "optimize X" wording.
- Do not add speculative architecture with no credible trigger.
- If the issue is current economic/security/data-loss correctness, do not hide it here to avoid a correction; fix it in the current phase.
- If it is scale-dependent, record the measurement and promotion threshold rather than prematurely redesigning.

---

# 15. Resolution log

Append immutable resolution records here when open items are closed.

| Date | ID | PR / commit | Evidence | Notes |
|---|---|---|---|---|
| 2026-09-10 | Initial backlog created | main `85226d4c...` | Review of Phases 0–5, PR reviews, current main source and PR #7 sync/Base Tap | Open items above are intentionally carried forward. |
| 2026-09-10 | PH-001 | Phase 7A | PostgreSQL lease/redrive/dead-letter implementation and processing regressions | Worker-loss blocker resolved before Clan event dependency. |
| 2026-09-12 | PH-003, PH-005, PH-006, PH-007, PH-042 | Phase 9 (base `bfe663b`) | PostgreSQL append-only triggers; qualification authority; source-time drain/freeze; atomic DLX ledgers/reconciliation tests | Scoring and growth distribution remain intentionally fail-closed pending approved policy. |
| 2026-09-12 | PH-005, PH-006, PH-007, PH-042 reopened | PR #13 review at `5233ed3` | Independent review found incomplete source-time, rebuild, freeze, carry, reconciliation and concurrency evidence | PH-003 remains accepted; reopened blockers require correction plus hosted green evidence. |
| 2026-09-12 | PH-005, PH-006, PH-007, PH-042 re-resolved | PR #13 hosted correction `9e025b9` | 480-test hosted PostgreSQL/Redis suite; immutable-history migrations; source-time qualification/drain; generation-safe rebuild; delayed carry; actual-ledger reconciliation; adjustment contention | Hosted CI was green. |
| 2026-09-13 | PH-005, PH-006, PH-007, PH-042 final verification | PR #13 follow-up to hosted `9e025b9` | 485-test local suite; Typing CREDIT source-time facts; claim-independent frozen inputs; score-producing-only Clan tie backfill | Marked VERIFYING until final hosted CI confirms the complete corrected HEAD. |
| 2026-09-13 | PH-006 Typing-referral attribution | PR #13 follow-up to hosted `485df50` | 489-test local suite; immutable Typing-referral source facts; source-time relation/tier provenance; later Referral ledger de-duplication | Final hosted CI is required before Phase-9 blockers return to RESOLVED. |

---

## Final rule

**MVP speed does not justify deferring correctness. Production hardening does justify deferring measured capacity tuning until the system is functionally complete.**

A future agent must therefore ask two questions for every discovered problem:

1. Can this cause wrong money/XP/DLX, duplicated or lost events, authorization bypass, corrupted state, unsafe migration, or unrecoverable operation under realistic current use?
   → Fix in the current phase as P0/P1.

2. Is the current design correct but capacity/latency/operations are uncertain until realistic scale exists?
   → Record/measure here and promote at its trigger.


# Phase 10 Hardening Backlog Delta (implementation review)

**Canonical reviewed baseline:** Phase-9 merge `4ca42b1c2be8bc815648f031a7bd7a8b1c226a36` (PR #13: 489/489 hosted tests, Phase 0, Foundation, PostgreSQL/Redis/RabbitMQ, migration drift, and Docker runtime green). The stale Phase-9 PH-005/006/007/042 verification states above are accepted as resolved.

**Phase-10 implementation evidence pending hosted PR gate:**

- PH-002: PostgreSQL publication trigger plus transaction-local service authorization; direct ORM/queryset/SQL publication is fail-closed. Final state remains subject to hosted PostgreSQL tests.
- PH-004: mission start/list/verify/claim now fail closed when DB verification/repeat policy diverges from the effective configured MissionRule; policy-bearing Admin fields are read-only after creation.
- PH-009: typed evidence resolution, explicit permission, deterministic purged/expired results, and durable per-view access audit were added.
- PH-026: publication chronology remains fail-closed. Domain-specific boundary preview enforcement needs final hosted evidence before this item can be marked resolved.
- PH-027: settlement-control events now retain actor/service actor, reason, ticket, correlation identity and before/after epochs; a PostgreSQL guard rejects direct field mutation.
- PH-028: WhiteNoise compressed manifest storage, STATIC_ROOT, image-build collectstatic, and DEBUG-independent production serving were added. Resolution requires hosted Docker/Admin asset smoke.
- PH-034: durable notification and broadcast recipient states provide rebuildable pending/processing/sent/retry/failed/uncertain/skipped metrics. Full SLO/alert/runbook work remains Phase 11.
- PH-045: an audited, locked manual-review operation supports confirmed revocation, still-unsafe retention, or safe known-credential revocation retry. Broader historical PH-045 questions remain open.

No Phase-11 backup, scale, final SLO, worker-sizing, season-close, or launch-hardening work was started.


## Phase 10 PR #14 correction delta

**Reviewed hosted HEAD:** `d049af43a3c9d18d4221e883413834d82125ae78` (Quality and Docker/runtime green; PostgreSQL suite 503 passed / 2 failed before this narrow correction).

The correction keeps the accepted PostgreSQL mutation guards and updates obsolete fixtures to use draft publication and settlement-control services. It adds notification channel convergence, deterministic broadcast completion, campaign-first cancellation/claim locking, expired cancelled-flight reconciliation, immutable active campaign definitions, service-backed moderation/config/settlement/invite Admin forms, mandatory suspension expiry including system actions, typed audited evidence viewing, manual notes, domain-specific safe-cutover validation, governance lifecycle notifications, and production Admin asset smoke.

PH-002, PH-004, PH-009, PH-026, PH-027 and PH-028 remain `VERIFYING` until the final hosted correction HEAD is green. PH-034 remains broader Phase-11 work while its Phase-10 delivery-state metrics slice is implemented. PH-045 remains open overall; only its Phase-10 locked/audited manual-review operation is under final verification. No Phase-11 work was started.


### PR #14 narrow final-correction backlog delta

- PH-026 evidence now covers old-and-new Daily reset boundaries, an open Base Tap accumulator, Typing attempt/wallet config snapshots (`test_claim_groups_original_config_versions_without_repricing`), Mission started/reward-ready snapshots (`test_mission_claim_snapshot_idempotency_and_config_boundary`), an open Creator aggregation window split by durable activity config, and active-Season DLX epoch boundaries.
- PH-009 test rendering uses non-manifest test storage only; production retains WhiteNoise manifest storage and its Docker Admin asset smoke.
- Broadcast Admin authorization now treats `notifications.manage_broadcasts` as authoritative for direct view/add/change URLs and keeps deletion disabled.
- New PostgreSQL threaded evidence covers Broadcast claim/cancel/lease/finalization races, suspension expiry vs BAN, and invite-risk resolution vs operators/revocation lifecycle.
- PH-002, PH-004, PH-009, PH-026, PH-027, and PH-028 remain VERIFYING until the final hosted correction HEAD is green. PH-034 remains OPEN overall; only its Phase-10 durable delivery metrics slice is implemented. PH-045 remains OPEN overall; its Phase-10 operational sub-requirement is under final hosted verification.


# Phase 11 Stage-1 hardening delta

**Canonical accepted baseline:** merge `0d1906c10ad2b94d6029f831937d79aba2cb192a`;
the final hosted Phase-10 suite passed **519 / 519**. PH-002, PH-004, PH-009, PH-026, PH-027 and
PH-028 are accepted as resolved by that merge and are not reopened without regression evidence.

**State vocabulary for this delta:** `IMPLEMENTED_PENDING_VERIFICATION` means code/tooling exists,
but Stage 2 must provide PostgreSQL, concurrency, drill, load, scan, or hosted-CI evidence before
promotion to RESOLVED.

| Item | Stage-1 state | Implementation / remaining evidence |
|---|---|---|
| PH-008 | IMPLEMENTED_PENDING_VERIFICATION | Durable provider registry, readiness report and persisted drain evidence; full close drill pending |
| PH-012 | IMPLEMENTED_PENDING_VERIFICATION | Configured Redis request-cost throttling before mutation DB work; benchmark pending |
| PH-015 | IMPLEMENTED_PENDING_VERIFICATION | Reward outbox counts, lag, retry and error-category metrics |
| PH-019 | IMPLEMENTED_PENDING_VERIFICATION | Global/method Bot API budget, retry-after scheduling and jitter; burst drill pending |
| PH-020 | VERIFYING | Typing errors typed; bootstrap query budget remains Stage-2 measurement |
| PH-022 | IMPLEMENTED_PENDING_VERIFICATION | Unsupported S1 lifecycle flag values rejected at publication |
| PH-023 | IMPLEMENTED_PENDING_VERIFICATION | Typing mission uses Typing reset timezone |
| PH-024 | IMPLEMENTED_PENDING_VERIFICATION | Stable expected Typing 409/429 responses and OpenAPI declarations |
| PH-025 | IMPLEMENTED_PENDING_VERIFICATION | Missed Daily period exposes effective reset streak without deleting history |
| PH-029 / PH-030 | IMPLEMENTED_PENDING_VERIFICATION | Production security injection and rotation runbook; security execution pending |
| PH-031 | IMPLEMENTED_PENDING_VERIFICATION | Guarded backup/restore and reconciliation commands; actual restore pending |
| PH-033 / PH-034 | IMPLEMENTED_PENDING_VERIFICATION | Operational snapshot and Stage-2 SLO worksheet; alert thresholds pending measurement |
| PH-037 | OWNER_ACTION_REQUIRED | Configure required GitHub ruleset; not changed by application code |
| PH-038 | IMPLEMENTED_PENDING_VERIFICATION | Structural contract-gate expansion remains subject to hosted gate |
| PH-039 | IMPLEMENTED_PENDING_VERIFICATION | Dependency/container report workflow added; findings not yet evaluated |
| PH-043 | OPEN — OWNER PRODUCT DECISION REQUIRED BEFORE S1 LAUNCH | **PH-043 IMPLEMENTATION BLOCKED BY PRODUCT DECISION**; no server evidence exists |
| PH-049 | IMPLEMENTED_PENDING_VERIFICATION | Bounded canonical Clan rebuild covers Treasury, Contribution, HQ/capacity, score and tie provenance; deliberate Stage-2 drift drill pending |

WATCH items PH-010/011/013/014/016/017/018/021/032/035 now have deterministic load manifests,
query EXPLAIN, relation sizing, provider-lag and reconciliation measurement primitives. All measured
capacity outcomes remain **MEASUREMENT REQUIRED IN STAGE 2**. No S1 launch item is promoted merely
because Stage-1 code exists.

## Phase 11 Stage-1 implementation-review correction

The finalization barrier, durable FAILED write, transient/manual drain scheduling, canonical bounded
RewardLedger projection rebuild, Season-scoped referral drain, append-only anomaly resolution,
amount-based DLX adjustment totals, stored Mission config provenance, service-backed Clan credential
disposition, final-standing-only closed ranking rebuild, and explicit trusted-proxy throttle identity
are `IMPLEMENTED_PENDING_VERIFICATION`. These corrections require Stage-2 PostgreSQL concurrency and
failure evidence before any item is promoted to RESOLVED. PH-043 and PH-037 remain unchanged.

The final Stage-1 correction additionally makes Player and Clan tie provenance a first-class
reconciliation invariant and treats pre-cutoff Telegram dead letters as manual work with immutable,
permissioned REQUEUE/DISPOSE evidence. Full concurrency and operational drills remain Stage 2.

## Phase 11 Stage-2 local verification delta — 2026-09-14

The PostgreSQL/Redis/RabbitMQ regression suite and focused barrier/dead-letter/security checks pass
locally. Real active-ranking Redis flush/rebuild, Telegram broker outage/redrive, Beat singleton,
production HTTP/static, dependency audit, narrow 50K Player load, and isolated 50K Player restore
evidence are recorded in `docs/PHASE11_STAGE2_EVIDENCE.md`.

PH-008, PH-031, PH-032, PH-033, PH-034, PH-039, and PH-049 remain
`IMPLEMENTED_PENDING_VERIFICATION`: their complete end-to-end, PostgreSQL 17, hosted, container,
economic restore, loss-matrix, or scale acceptance evidence was not available. PH-037 remains
`OWNER_ACTION_REQUIRED`. PH-043 remains `OWNER PRODUCT DECISION REQUIRED BEFORE S1 LAUNCH`.

## Phase 11 final correction delta

Owner-supplied hosted evidence establishes PostgreSQL 17.11 migration, Contract Gate, Foundation
quality/test/Docker runtime/Admin/static success, and 534 hosted tests at correction baseline
`43eedff187394dce3ff767224d8795136d6e2977`. The final correction adds a complete six-epoch
close/failure/resume/barrier campaign, real DLX allocation/carry/residual/hold evidence,
CLOSED/ARCHIVED immutable Redis rebuild checks, worker before/after-commit duplicate-delivery checks,
and a complete PH-049 projection-column drift test. PH-008 and PH-049 are `VERIFYING` pending the new
hosted correction run rather than being prematurely marked RESOLVED.

PH-039 remains `VERIFYING`: the broken Trivy action reference is corrected from `0.28.0` to the valid
upstream `0.36.0`, but the resulting hosted SARIF artifact must be inspected before resolution.
PH-031 is `VERIFYING`: a representative complete economic database backup/restore matched counts and
checksums across rewards, projections, Clan ledgers, six epochs, DLX allocations/adjustments/residuals,
final standings, medals, and Telegram disposition evidence. PH-033 remains
`IMPLEMENTED_PENDING_VERIFICATION` for its remaining hosted notification process-kill matrix, while
economic before/after-commit, duplicate delivery, stale lease, RabbitMQ redrive, and Beat singleton
evidence pass. PH-037 remains `OWNER_ACTION_REQUIRED`; PH-043 remains
`OWNER PRODUCT DECISION REQUIRED BEFORE S1 LAUNCH`.
