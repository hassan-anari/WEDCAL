# Dexo Land — Cross-Repository Integration & Release Execution Contract

**Contract ID:** DLX-INT-2026-10-14  
**Version:** 1.1 (batch-delivery amendment proposed for owner review)  
**Status:** Active execution contract — original PR #22 merged; batch delivery amendment proposed in a separate documentation PR  
**Owner decision date:** 2026-10-14  
**Owner repositories:**
- Frontend: https://github.com/hassan-anari/Dalexio-Game
- Backend: https://github.com/hassan-anari/Dalexio-Game-Backend
- Existing production API: https://api.dalexio.com (backend `main` is automatically deployed)

## 0. Purpose, scope, and reading order

This is the durable **cross-repository execution plan and handoff contract** for turning two substantially implemented but only partially integrated codebases into a coherent Season 1 Telegram Mini App. It is intended for the owner, architects, Codex, other coding agents, reviewers, and release operators. It is **not** a request to implement the entire plan in one PR.

Every future integration agent must read:
1. Backend `AGENTS.md`, including branch/CI/owner-approval rules.
2. Backend `DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md`.
3. Backend `DEXO_LAND_SEASON1_ECONOMY_CONFIG_CONTRACT.md`, effective versioned game config and `PRODUCTION_HARDENING_BACKLOG.md`.
4. This contract and its current execution register.
5. Both repositories' current source and tests for the assigned vertical slice; frontend `README`/routing/design-system guidance, where applicable.
6. Relevant previous PR reviews, actual API contracts and the latest CI evidence. Do **not** infer current behavior from old screenshots, fixture data, or this snapshot alone.

**Authority and conflicts.** Existing approved source-of-truth documents retain authority over all unchanged Season 1 rules. **Section 2 below records the owner's later, explicit changes to Base Tap**. Those changes *supersede the former Base Tap-specific business intent*, but the existing canonical source-of-truth and economy documents still contain incompatible text. **Gate C-01 (controlled contract reconciliation) must update those canonical documents, schemas/config and tests before production code for the new Tap economy is merged.** No agent may silently ignore contract tests, edit historical published configurations, or rewrite unrelated economy rules to make tests pass. This document defines integration sequencing, QA and handoff; the canonical product/economy documents define the rest of S1.

**Current baseline:** Frontend is React/TypeScript/Vite/TanStack Query/Zustand; backend is Django/Django Ninja/PostgreSQL/Redis/Celery. API under `/api/v1` with signed Telegram Mini App initData authentication. Bootstrap, Base Tap and parts of Profile/Notifications are partially connected; most Earn, Typing, Referral/Network, Clan and Arena UI is still local/demo state. This assessment is a planning snapshot, **not a claim that integration tests have passed**. Re-check both current heads before each phase.

## 1. Immutable program rules

- **No direct pushes or merges to production `main`.** Backend `main` automatically deploys. Work on short-lived branches; separate PRs; all necessary CI and independent reviews; **explicit owner approval** before merge or the next phase. Create draft PRs when incomplete.
- Two repositories mean **two independently reviewable histories, test suites and PRs**, even if one agent can edit both workspaces. Never imply one cross-repository atomic Git commit exists.
- Prefer **contract-first, backend-compatible-first, then frontend migration**, followed by cross-repo E2E before activating new behavior for production users. Keep old-client compatibility or a proven gated cutover; don't break live clients on backend deploy.
- Do not change approved visual design/navigation, touch feel, animations, accessibility or Telegram WebView behavior except where necessary and specifically reviewed. Preserve S1 scopes/locked S2 previews.
- **Server-side authorization and PostgreSQL durability** for all economics, membership, claims, levels, rankings and governance. Redis is a disposable acceleration/cache/rate-limiting layer, never the only economic authority.
- Atomic, audited, idempotent economic changes; strictly versioned/config-driven business numbers; preserve history and reconcile projection/outbox dependencies.
- No client-supplied player ID, role, number of earned points, cooldown timestamps or tap-count claims can be treated as inherently trustworthy.
- Every integration phase includes typed API response/error contracts, loading/empty/error/retry UX, reproducible tests, performance measurements where relevant and observability.
- No unauthorized production data writes, migrations, secret reads or live load tests. Staging/synthetic data first; explicit operational approval for controlled production verification.
- Every PR must include exact modified paths, API/schema changes, migrations, rollout/rollback, test commands/results, CI links, unresolved risks and a handoff entry (section 10).
- Existing release blockers remain visible: **PH-037** branch protection/required checks and **PH-043** `read-season-brief` product evidence; don't claim S1 launch readiness while they remain open.

## 2. Owner-approved Base Tap product change — superseding prior Tap-only semantics

**Decision BT-01 — Unlimited earning:** No UTC-day tap count cap, no daily reset or daily remaining/reward cap for Base Tap. The UI 50-tap figure is **a per-burst allowance**, not a per-day allowance. A user may complete unlimited eligible bursts over the playable season, subject to cooldown, session validity, gameplay availability, moderation and anti-abuse controls. **Daily Reward and Daily Streak are separate products and remain unchanged.**

**Decision BT-02 — Immediate presentation:** Every eligible physical touch—including concurrent multi-touch pointers—must instantly drive the onscreen tap effect, XP counter, level threshold/progress, level-up and Dexo evolution without waiting for an HTTP round trip. No artificial 100–250 ms wait for visible XP. Base Tap should not display a pending-points economy or later subtract otherwise valid awarded taps due to a hidden daily cap.

**Decision BT-03 — Server-authoritative and immediately durable:** The backend must commit accepted tap rewards to **RewardLedger, PlayerProgression, PlayerSeason** and relevant durable economic provenance in the request transaction, with canonical balance/progression derived by its established reward primitive. No end-of-day XP settlement for new Base Tap awards. Async after-commit work is allowed only for noncritical downstream fan-out. No separate PostgreSQL write required for each physical touch: bounded micro-batches are allowed.

**Decision BT-04 — Cooldown:** Preserve the approved existing experience of **up to 50 taps per burst followed by a 10-second cooldown** as the initial baseline; represent the rules in versioned server config. The server owns the remaining allowance and unlock time, across refresh, replay and multiple devices. Cooldown is **not** a daily earning cap. These values are the owner's current UX baseline, not permission to hardcode them in domain logic.

**Decision BT-05 — Server-authorized instant UX:** Prefer **server-issued bounded tap leases/authorizations** that contain session, player, season, immutable config version, allowed burst, reward rate, validity and replay protection. An active valid lease permits the frontend to update the screen immediately, while a durable client outbox transmits individually numbered tap intents as micro-batches. The backend independently verifies and consumes lease allowances and atomically settles acknowledged increments. No new optimistic credit when lease is absent, expired, invalidated or cannot be safely recorded for recovery.

**Critical physical limitation / required acceptance wording:** Zero-latency **UI change** and synchronous **remote database commit** at the same instant cannot both be guaranteed over a network. A lease is prior server authorization, **not evidence that unsent taps have already committed**. A client can lose untransmitted events with catastrophic device/storage failure; bot detection cannot prove a real human finger from HTTP messages. Therefore:
- Claim **instant authorized display, immediate transactional server settlement, and no routine negative reconciliation of valid taps**, not an impossible promise of literally zero network-time durability.
- If the owner requires absolute server-final XP on the exact touch frame even if the device dies before transmission, that requirement is physically incompatible with a remotely authoritative database and requires an explicit product trade-off; agents must disclose, not conceal, this distinction.
- Do not silently decrease valid visible XP to handle server-side daily caps; that cap is removed. When uncertain, stop issuing further credit and recover/replay safely. A rejected/invalid/revoked lease, malicious events or destroyed unsent outbox are exceptional cases that require deliberate, transparent UX—not invented guaranteed credit.

**Decision BT-06 — Economical transport and server safety:** Adaptive micro-batching, bounded payloads, request coalescing, keyed idempotency, in-flight limits, retries with backoff, and a server-owned session/lease ledger. Tune intervals and batch sizes by measurements—not hardcoded guessed SLOs. HTTP request/response is sufficient initially; WebSocket is not a prerequisite. Respect ingress throttle and player/season write contention. Keep the confirmed Point-to-DLX and referral-share exclusions exactly as existing S1 contracts say (Tap yields no direct DLX and no passive referral share).

**Decision BT-07 — Economy change requires broader regression:** Re-evaluate reward ledger growth, snapshots, epoch `CORE_GAMEPLAY` contribution, leaderboards, season-close barriers, anti-fraud, moderation, outbox delivery, expiry/cleanup, configuration cutovers and balance assumptions. Unlimited Tap earning materially changes balancing; introduce an owner-reviewed configuration version and telemetry-based inflation/abuse thresholds before public rollout. **Do not mutate published GameConfigVersion snapshots or backfill settled epochs.** Season eligibility still applies even without a daily cap.

### 2.1 Proposed Tap contract (design candidate; finalize during C-01/P02)

Propose typed `GET /api/v1/base/tap-state` (or documented equivalent), a lease/session issuance/renewal endpoint and idempotent `POST /api/v1/base/taps`. Response shape is a contract target, not a license to independently deploy breaking routes:
- Authenticated authoritative `player_id` (server resolved), `season_id` and `config_version`.
- Opaque `session_id` / `lease_id`; lease expiry and version; `server_time`, `burst_remaining`, `cooldown_until`, `allowed_batch_size` and `reward_per_tap`.
- Batch request: opaque lease reference, monotonically numbered tap events/range, bounded positive count and unique idempotency key; **never** client-asserted earnings or raw identity.
- Batch response after DB commit: `accepted_taps`, any rejected/duplicate counts with stable reason codes, receipt/sequence cursor, `committed_lifetime_points`, `committed_season_points`, `level`, `level_progress`, `dexo_stage` and updated server cooldown/lease status.
- Explicit retry-safe contract for lost responses, app restart, revoked lease, out-of-order replies, parallel requests/devices, season rollover and config changes. Session/lease counters must not be double-consumed.
- Display calculations use **the server-authorized config snapshot**, including thresholds and Dexo stages. Server snapshots carry monotonic version/sequence; stale replies must never overwrite newer state.
- A bounded, durable on-device outbox is a UX/recovery mechanism, not an economic source of truth; storage loss and lease expiry have documented failure semantics. Accepted server receipts and ledger remain authoritative.

### 2.2 Tap migration and cutover safety

1. Inventory historical `BaseTapDailyAccumulator` pending awards, receipts, frozen epoch facts and existing settlement jobs; reconcile and **settle/close legacy pending balances exactly once** before retiring that writer. No deletion of unsettled economic obligations.
2. Amend old Tap sections (Source of Truth `57.4`, Economy Contract `26`, machine-readable sample, typed config/schema validators, tests, production hardening risk register). Publish a new **effective-dated** config rather than editing existing published config.
3. Add a separately gated/new-version Tap path compatible with current deployed clients. Never simultaneously run new immediate settlement and old finalizer against the same economic event; unique provenance/idempotency is required.
4. Validate in PostgreSQL under concurrency/fault injection; demonstrate accepted XP is committed when response returns and that an identical retry never mints XP twice.
5. Transition frontend after backend support is deployed and validated; activate only when two-repo E2E succeeds. Observe lag/reject/duplicate/429/ledger-volume metrics. Retire legacy path only after safe-drain evidence.
6. Document concrete rollback **without double reward**, ledger rewrites or loss of already accepted tap intents; rollback might mean disabling issuance of new leases while preserving redemption of already authorized valid leases.

## 3. Telegram launch, VPN and authentication reliability

**Decision AUTH-01:** A Mini App genuinely opened in Telegram must not be labeled "outside Telegram" simply because the externally hosted SDK failed to load. Current frontend `index.html` loads `https://telegram.org/js/telegram-web-app.js`; `src/integrations/telegram/adapter.ts` labels missing/empty launch context as browser. This is a **credible failure hypothesis under filtered connectivity**, not a proven sole root cause.

Required investigation: reproduce on actual Telegram iOS/Android/Desktop WebViews across representative VPN-off/on networks. Capture privacy-safe diagnostics for SDK load status, initial `Telegram.WebApp` availability, signed `initData` availability, app origin/asset reachability, API reachability, actual HTTP/JS failures and cold launch/relaunch timing; **never log raw initData, bot secrets, auth hashes or unredacted PII**.

Target:
- Self-host a **pinned, vetted copy of the official Telegram WebApp JS bridge** on the Mini App's reachable origin (verify compatibility and update process); avoid runtime hard dependency on filtered `telegram.org` asset delivery.
- Implement resilient launch/context initialization and bounded retry/recovery; distinguish `SDK_LOAD_FAILED`, `LAUNCH_DATA_MISSING`, `AUTH_INVALID`, `AUTH_EXPIRED`, `API_UNREACHABLE`, `NETWORK_TIMEOUT` instead of displaying one inaccurate Telegram-only error.
- Investigate standard Telegram LaunchParams recovery as a compatibility fallback. Only server-verified signed raw `initData` authenticates API requests; neither `initDataUnsafe` nor URL/user IDs are security principals.
- Ensure onboarding, profile, every initial protected fetch and cold deep-link route pass Telegram credentials correctly; no Vercel proxy/API authentication regressions.
- A self-hosted bridge alone cannot solve inaccessible Mini App/CDN/API origins. Treat provider/network accessibility and Telegram version matrix as separate proof gates.

## 4. Cross-repo integration inventory and ownership

| Product slice | Frontend starting point | Backend starting point | Required result |
|---|---|---|---|
| Boot / auth | `index.html`, `integrations/telegram/adapter.ts` | `apps/telegram/auth.py`, `mini_app_auth.py` | Verified reliable Telegram launch, correct error states |
| Bootstrap / identity | `integrations/api/bootstrap.ts`, `queryKeys.ts`, onboarding | `/bootstrap`, progression/player/season | Fully typed server data, no stale cross-user/season caches |
| Base / Tap | `game/base/useBaseTapExperience.ts`, `useBaseTapBatcher.ts`, `baseTapLocalState.ts` | `apps/base_tap/*`, `apps/rewards/services.py` | Instant authorized UI, real transactional rewards, no daily cap |
| Daily / streak | `pages/EarnPage.tsx`, `game/earn/earnStore.ts` | `/daily`, `/daily/claim` | Persistent server claim/streak/cycle |
| Missions | Earn local missions | `/missions`, `/{key}/start`, `/verify`, `/claim` | Server-verifiable mission states and rewards |
| Typing | `pages/TypingChallengePage.tsx` | `/typing`, attempts start/submit/claim | Server-issued phrase, server score and idempotent claims |
| Referral | Earn / Network sample data/link | `/referrals` | Server-issued codes/links, correct attribution and verified rewards |
| Creator / Network | Earn / Network mock cards | `apps/network/*`, ingestion/rules | Real verified activity/claimable states |
| Profile / collection | `pages/ProfilePage.tsx` and local stores | `/profile/*` | Canonical lifetime, achievements, cosmetics/review |
| Notifications | Header partial remote wiring | `/notifications/*` | Consistent read/unread and deep link behavior |
| Arena | `pages/ArenaPage.tsx` and mock ranks | `/arena/rankings/*`, `/arena/dlx` | Server rankings; correct locked S2 Warfront and off-chain DLX |
| Clan core | `pages/ClanPage.tsx`, local `clanStore` / catalog | `/clans/state`, role/create/applicants/invite/membership | Genuine Captain/Soldier lifecycle, Telegram eligibility |
| Clan governance | Incomplete UX | `/clans/governance`, `/recovery`, `/removal-cases`, votes, leave/dissolution | Complete recovery, votes, emergency exit paths |
| Clan economy | Local inventory/shop/Treasury | `/clans/treasury`, `/shop`, `/inventory`, `/missions` | Server money/missions/HQ effects, no local minting |
| Release | Frontend lacks equivalent full CI; backend main auto-deploy | CI/production workflows, hardening backlog | Both-repo gates, staged cutover, measured rollback |

**Known integration mismatches requiring explicit tests:** frontend referral link constructed from Telegram user ID rather than backend-issued code; cold onboarding/profile bootstrap calls may omit `initData`; frontend Bootstrap adapter currently discards rich server feature payloads; query caches not consistently player/season keyed; Clan UI exposes local-only operations and lacks backend governance/recovery; Base Tap UI today uses UTC-day local state, local burst cooldown, optimistic XP and a server daily cap—causing reversals. Re-verify these observations against current code before fixing.

## 5. Batched integration delivery roadmap (owner-approved execution gates)

**Owner delivery-model update: 2026-10-14.** The detailed labels `C-01` and `P-01` through `P-13` below are **workstream/checklist identifiers**, **not separate Codex task or release gates**. Execute related workstreams together inside **six integrated batches**. For this integration program, a **Batch is the development phase** governed by the `AGENTS.md` mandatory PR/CI/independent review/explicit owner-approval rule. The completed `C-00` bootstrap is historical preparation. **Do not create an independent C-01-only task by default.** Reconcile Tap documentation and implement its related runtime and UI pieces as the coordinated **B2** deliverable, in safe internal order, while preserving separate review of any unresolved product decisions.

| Batch / owner gate | Workstreams grouped into a single coordinated objective | Delivery result |
|---|---|---|
| **B1 — Foundation & Telegram Authentication** | `P-01` + `P-04` + relevant frontend test-discovery/E2E baseline | Reliable Telegram launch with VPN-off diagnostics/robust SDK delivery, authenticated Bootstrap/client API/typed contracts, player/season-scoped cache, meaningful errors and reproducible QA |
| **B2 — Real-Time Tap Economy** | `C-01` + `P-02` + `P-03` | Canonical uncapped Tap contract, compatible backend atomic rewards and leases, immediate multi-touch XP/Level UI, server-owned 50-tap-burst/10s cooldown, historical pending settlement migration, abuse and load evidence |
| **B3 — Earn & Network** | `P-05` + `P-06` + `P-07` | Complete Daily/Missions/Typing/Referral/Creator/Network server-backed flow and one coherent progression/cache integration |
| **B4 — Profile & Arena** | `P-08` + `P-09` | Server-sourced Profile/collection/achievements/notifications, player/clan ranking and correct confirmed off-chain DLX presentation |
| **B5 — Complete Clan System** | `P-10` + `P-11` + `P-12` | End-to-end Captain/Soldier membership, Telegram-group verification, governance/emergency exit/recovery, Treasury/shop/inventory/missions with role/economic concurrency proof |
| **B6 — Integrated Release & Hardening** | `P-13` + remaining PH-037/PH-043 owner launch gates | Staging cross-repo E2E, Android/iOS Telegram WebView, security/load/recovery/rollback, CI branch protection and controlled deployment proof |

**Default execution model per batch:** Two coordinated Codex Cloud tasks—one based on the **currently active backend repository** and one on the **private frontend repository**—with shared request/response/error schemas, named endpoint owners, pinned base/head SHAs and an explicit integration gate. Each agent should own the **entire repository-side scope of its batch**, not receive separate micro-tasks for individual pages, endpoints, or test assertions. When one repository needs no runtime changes, its task may be read-only audit/contract verification; **never force unnecessary edits**. Target **one coherent Draft PR per repository per batch when safe and reviewable**. Split only for warranted migration, deployment-compatibility or independent security/release gates, keeping them under the same Batch ID and cross-linked. **Do not merge any individual PR** without owner approval, even if it is part of a bigger batch.

**Within-batch sequencing:** Agents may complete multiple logically related subtasks before final handoff; do not stop after each old `P-xx` label. But perform safe contract/compatibility preparation before breaking changes, and obtain an owner decision for any unresolved economics or security invariant **before encoding that decision in runtime code**. Backend `main` automatically deploys to the live API; stage backward-compatible backend behavior behind a disabled/controlled feature before migrating frontend clients. Frontend and backend PRs may be developed concurrently against a pinned contract, but deployment activation is controlled and proven safe.

**Batch acceptance:** Both repository PRs (when applicable) are inspected; required checks pass; real PostgreSQL/transaction tests support economic work; frontend unit and appropriate browser/Telegram tests pass or are explicitly blocked with separate evidence; the pinned frontend/backend pairing has been tested in staging or an approved isolated harness; rollout/rollback and economic obligations have been reviewed. **Only owner approval opens the next batch.** Passing an API connectivity check to `api.dalexio.com` or `dalexio.com` does not authorize production account/reward mutations or load tests.

**Environment results already observed:** In one Codex Cloud task, GitHub connector access could read/write both repositories and create independent documentation Draft PRs (backend `#24`, frontend `#1`). Local backend and frontend worktrees **could not be jointly built/tested in that environment**: shell Git transport hit proxy port 8080, the frontend environment had no backend checkout/services, and frontend web server binding failed with `EPERM`. Two separate repository environments plus cross-repository CI/staging therefore remain the selected operating model. Frontend Vitest/Playwright discovery and stale E2E expectations are part of **B1** quality baseline, not a license to disable failing tests. Backend repository `WEDCAL` is a temporary name; don't rename remotes, application configuration or documents solely because of that temporary change.

**Execution statuses:** `PLANNED` → `IN_PROGRESS` → `PR_OPEN` → `CI_GREEN` → `OWNER_APPROVED` → `MERGED` → `ACCEPTED`. `BLOCKED` requires a documented owner decision. Update the Batch register in section 9 and the affected historical workstream notes after each delivery. No auto-advance.

### Detailed workstream acceptance catalog (grouped within B1–B6; not standalone owner gates)

### C-00 — Establish contract, repository controls and cross-repo access

**Status:** ACCEPTED (contract PR #22 merged; connector-based dual-repository publication tested in backend #24 and frontend #1; local joint build not verified).  
**Scope:** Make this plan discoverable from backend `AGENTS.md`; confirm frontend/backend heads, current API and environment. Run docs-only Codex Cloud dual-repo capability probe (section 8). Identify backend `main` auto-deployment, required checks, frontend CI deficiency, safe branch/PR permissions and PH-037 owner action.  
**Deliverables:** reviewed contract PR, recorded Codex capabilities, API/version/feature-flag ownership, release governance.  
**Gate:** owner approves/merges contract and any minimum agent discovery changes. No functional changes or production writes in this phase.

### C-01 — Reconcile changed canonical Base Tap contracts before changing economics

**Status:** PLANNED. **Depends:** C-00 accepted.  
**Backend:** Prepare a focused contract PR updating Season 1 Source of Truth `57.4`, economy `26`, sample config, validator/API schema intentions, hardening backlog and invariants for uncapped per-burst Tap. Retain history of old published config. Specify exact migration of pending legacy XP, reward provenance and Season close behavior. Obtain owner agreement on config values and risk metrics.  
**Frontend:** Review existing 50-tap/10s motion design and Level/Dexo threshold inputs without changing visual design.  
**Gate:** canonical documents and contract CI consistent; old/new semantics precisely versioned; no unintended Daily Reward/Referral/DLX changes.

### P-01 — Telegram launch/auth resilience (first functional integration PRs)

**Status:** PLANNED. **Depends:** C-00.  
**Frontend:** Pin/self-host verified Telegram JS; robust bootstrap/LaunchParams fallback; correct route-level auth propagation; explanatory recoverable errors and retry; protect against stale data from previous Telegram user.  
**Backend:** Modify only if reproduced errors demonstrate a backend deficiency; preserve Telegram signature/expiry verification and canonical identity.  
**Tests:** SDK unreachable from `telegram.org` but app origin reachable; VPN-off/on Telegram WebView matrix; fake web browser; missing/expired/tampered initData; network/offline/timeout; deep-link cold launch; no PII in logs.  
**Gate:** no false "outside Telegram" for a valid Telegram launch in supported tested environments; security checks remain enforced.

### P-02 — Tap transactional backend, leases, migration, compatibility

**Status:** PLANNED. **Depends:** C-01 and P-01 authentication baseline.  
**Backend:** Introduce versioned burst/cooldown lease/session policy and state; bounded batch validation; canonical reward ledger + progression + season projection in the *same commit*; idempotent receipts and stable authoritative snapshot; concurrency serialization and safeguards; anti-bot/abuse controls independent of daily XP caps; metrics. Reconcile and drain existing `BaseTapDailyAccumulator` before destructive cleanup. Keep old deployed clients compatible behind rollout gates.  
**Tests:** duplicate/lost response, concurrent batch, parallel device, burst cap/cooldown, repeated bursts crossing UTC midnight, no daily cap, config/season boundary, grace/recovery, replay, rollback/outbox, transaction failure, moderation, ranking/epoch consistency, PostgreSQL production-like locking.  
**Gate:** no pending new Tap awards, no per-day cap, no double mint, no daily finalizer dependency for new events, measurable database cost and safe rollback.

### P-03 — Tap instant frontend and end-to-end protocol

**Status:** PLANNED. **Depends:** P-02 backend compatibility deployed behind disabled/gated feature and validated.  
**Frontend:** Pointer-safe multi-touch without duplicate events; leased immediate XP/Level/progress/Dexo stage animation; local durable sequence outbox and bounded microbatch sender; ordered authoritative ack reconciliation; cooldown server-time sync after refresh, network disruption and device switch. Remove `todayPoints`/`lifetimeFloor` math and daily Tap resets from economic display. No mixed local/mock production XP.  
**Tests:** no visual latency waiting on HTTP; 50 taps then 10s cooldown in owner-approved baseline; concurrent fingers; 51st touch blocked; unlimited successive bursts; level boundary and Dexo change within one batch; multiple devices; low-bandwidth/restart/offline recovery; lost ack/replay, stale response and 429, no ordinary negative XP rollback.  
**Gate:** cross-repo contract E2E against staging + measured concurrency/load. Owner approves UX recording and performance envelope before flag rollout.

### P-04 — Shared integration foundation, Bootstrap and cross-user cache isolation

**Status:** PLANNED. **Depends:** P-01; can be prepared before P-03 only if no conflict.  
**Frontend:** Typed API adapter/error taxonomy; validated server bootstrap for all exposed rich payloads; query keys scoped by authenticated player, season, config; invalidation on identity/season transitions; clear split between demo scenario mode and production; loading/error/empty/retry.  
**Backend:** Publish stable typed Bootstrap/OpenAPI error codes and cheap snapshot endpoints only where required.  
**Gate:** first launch, navigation, reload, Telegram-account switching, config/season turnover, protected routes, data isolation and responsiveness.

### P-05 — Daily Reward and Missions

**Status:** PLANNED. **Depends:** P-04.  
**Scope:** Wire backend `/daily` and mission state/start/verify/claim APIs, server rewards and streak; map statuses into the approved Earn UI. Eliminate production use of `earnStore` for canonical points, claims or missions. Keep Daily's UTC reset **independent from Base Tap**.  
**Gate:** duplicate/concurrent claim idempotency, expired/failed verification, reinstalls/reloads, streak and XP coherence across Profile/Base/Arena.

### P-06 — Typing Challenge

**Status:** PLANNED. **Depends:** P-04/P-05 unified progression.  
**Scope:** Replace local phrase/scoring/reward with server attempt start, issued text, typed response submission and claimable reward state; preserve 30s Mini App UX and server-defined attempt limits.  
**Gate:** timer/client clock manipulation, replayed attempt, race, duplicate claim, interrupted app, rewards reconciled.

### P-07 — Referral, Creator and Network

**Status:** PLANNED. **Depends:** P-04.  
**Scope:** Use backend-provided referral code/deep link (not Telegram numeric user ID), referral status/verification; wire network/group/speaker/creator participation and real verified activity. Never invent attributed referrals client-side.  
**Gate:** new-user invitation chain, self-invite prevention, verified attribution, rejection/review, delayed ingest, moderation, eventual consistency, no fake earnings.

### P-08 — Profile, Achievements, Collection, Notifications

**Status:** PLANNED. **Depends:** P-04, canonical rewards.  
**Scope:** Profile overview, `lifetime_points` (not merely current XP), level/Dexo stage, achievements, medal/collection, customization/review; notification read/unread and safe deep links. Remove demo store as production authority. Keep wallet/account unavailable where Season 1 explicitly excludes them.  
**Gate:** refresh/device consistency, authenticated data isolation, unlock boundary, idempotent edits, UI no demo fallback on errors.

### P-09 — Arena Player/Clan Rankings and DLX presentation

**Status:** PLANNED. **Depends:** P-04, relevant Clan snapshots; can split Player rankings earlier and Clan rankings later.  
**Scope:** Replace fixture ranks with server `/arena/rankings/players`, `/clans`, `/arena/state`, `/arena/dlx`; respect frozen/settled epoch accounting; show DLX as confirmed off-chain allocation only, not claimable/on-chain or a USD price. Preserve S2 Warfront locked.  
**Gate:** tie/rank ordering, paging, archived season, stale projection invalidation, no fake DLX balance.

### P-10 — Clan Core: role, discovery, group and membership

**Status:** PLANNED. **Depends:** P-04/P-08 profile identity and approved backend contract.  
**Scope:** Implement server-sourced `/clans/state`/`home`, Captain/Soldier choice, verified Telegram group eligibility, create, search/discover, join requests, applicant decisions, invite and membership states; map all loading/pending/rejection/Telegram-admin loss states. Preserve exactly two S1 roles, no Officer. Document internal group PK versus `telegram_group_id` naming ambiguity and surface missing membership/request IDs needed by UI.  
**Gate:** cross-user and cross-clan authorization, unique membership/request, capacity, recovery after refresh, bot permissions, Telegram verification, race/duplicate decisions.

### P-11 — Clan Governance, emergency exits, recovery and dissolution

**Status:** PLANNED. **Depends:** P-10.  
**Scope:** Real backend-driven leave restrictions/cooldowns, Captain removal case (no arbitrary kick), Soldier votes/quorum, emergency/recovery case, Captain inactivity, lost group admin privileges, dissolution decisions, Telegram leave/ban reconciliation, rejoin states and notifications. Show full decisions in existing UI rather than inventing simplified rules.  
**Gate:** role enforcement, full state-transition matrix, multi-actor concurrency, deadline/rejoin, Captain-only clan, irreversible operations and rejection paths.

### P-12 — Clan Economy, HQ, Shop, Inventory, Missions and contributions

**Status:** PLANNED. **Depends:** P-10/P-11, unified rewards.  
**Scope:** Canonical Treasury, personal contribution, HQ, Shop purchase, inventory activation/effects, Clan missions/trophies. Extend read APIs narrowly if UI requires missing details; no frontend-minted Treasury/inventory effects. Maintain exact economic splits, audit, refund/failed purchase behavior and season-close integrity.  
**Gate:** concurrent purchases/activations, negative balances prevented, permission checks, ledger totals, stale inventory, transition after Captain/recovery changes.

### P-13 — Integrated QA, operational proof and controlled S1 launch

**Status:** PLANNED. **Depends:** all in-scope phases accepted.  
**Scope:** Both-repo type/lint/unit/component/contract tests, hosted real PostgreSQL integration, Playwright/mobile Telegram WebView E2E and abuse/security matrix; staging end-to-end telemetry; latency and DB-lock/ledger-growth benchmarks at representative active concurrency; schema drift, dependency scan, regression of six-epoch DLX freezes and Season close. Validate version skew and rollback. Establish frontend CI, backend branch rules (PH-037), resolve PH-043 and owner launch checklists.  
**Gate:** owner signs off UX, economic integrity, performance budgets **measured and approved**, rollback, incident response and production monitoring. Public rollout uses controlled flags/canary, not blind deploys.

## 6. Required contract and acceptance matrices

**Cross-cutting behavioral cases** must be attached to the phase PR, not deferred indefinitely:

| Category | Minimum proof |
|---|---|
| Authentication | Missing/forged/expired Telegram signature rejected; valid Telegram launch not misclassified by SDK/CDN failure |
| Account and season | No cross-user or prior-season cache leakage; daily vs lifetime point semantics preserved |
| Atomic economy | RewardLedger/PlayerProgression/PlayerSeason agree at COMMIT; failed transaction changes none |
| Idempotency | Same request/key/event sequence never rewards twice; conflicting replay is rejected |
| Concurrent actors | Same player/tap session/Clan action on multiple devices does not violate invariants |
| Network | Timeout after commit, retries, slow connection, dropped responses, 429, offline recovery |
| Gameplay UI | Instant authorized Tap/level/progress/Dexo animation; no hidden day-cap reversal |
| Abuse | Server owns cooldown and balances; tampered client inputs, rapid replays, bots and proxy abuse bounded |
| Config/time | Effective-dated config, UTC day boundaries for Daily only, epoch/season close, source-time fidelity |
| Release | Old frontend vs new backend; new frontend vs gated backend; safe disable/rollback and economic drain |
| Telegram | Android/iOS/Desktop versions, VPN-on/off network matrix, cold launch/deep link, haptic/safe areas |
| Data provenance | No fake referral, rankings, Clan states, balances, trophies or DLX valuations in production |

**Performance rule:** Set numeric SLOs only from a documented baseline and owner-reviewed load profile (representative concurrent players, tap rate, HTTP RPS, p50/p95/p99 request→commit latency, p95 input→paint, DB lock waits, ledger growth, server CPU/IO and retry backlog). Fast visual response must not be confused with remotely durable acknowledgment. Do not assert unsupported 50K/1M capacity.

## 7. Production deployment, contract rollout and rollback

Backend code on `main` automatically deploys to the live API. This changes acceptable PR ordering:

1. Contract updates / migrations that are backward compatible and safe to deploy with old frontend.
2. Additive endpoints or fields, behind server-controlled opt-in; no immediate incompatible default.
3. Backend staging smoke, concurrency, monitoring, finalizers/pending obligation migration.
4. Frontend PR consuming new contract; staging end-to-end and mobile WebView validation.
5. Owner authorizes frontend release and later server feature-flag enablement/canary.
6. Observe first cohort; only then retire old API/background settlement; preserve historical ledger and season snapshots.

All migrations are forward-safe and rollback-tested on backups/synthetic copies. Never "undo" earned points by rewriting historical ledger. If operation is unsafe during Season close/epoch freeze, pause at the barrier and document the owner decision. Production feature flags must have a proven off switch and bounded lease recovery behavior.

## 8. Codex Cloud multi-repository capability probe (completed; docs-only)

**Completed 2026-10-14:** One Codex Cloud task successfully created independent documentation-only branches/commits/Draft PRs using the authorized GitHub connector in both repositories (backend #24, frontend #1). This verified API-based cross-repo publication, **not two local worktrees or local integration tests**. Shell Git transport failed through proxy port 8080, and a separate frontend-environment audit confirmed no backend checkout/runtime. Adopt **two coordinated Codex Cloud environments** and pinned CI/staging cross-repo acceptance. The original test requirements below are retained for audit history.

**Test footprint:** Create a unique, nonfunctional Markdown file such as `docs/codex-multirepo-probe/20261014-<short-run-id>.md` in **each** repository, with: repository name, timestamp, base SHA, task/run ID, confirmation this is documentation only. One distinct branch+commit+draft PR **per repository**, targeting `main`; absolutely no change to application code, CI/config, secrets or AGENTS. Do not merge.

**Report independently per repo:** readable? writable workspace? permitted git branch/commit? GitHub publishing/PR creation? PR URL and commit SHA? If secondary checkout is unavailable, or credentials/sandbox forbid write/publish, stop for that repo and report the exact boundary; do not use unauthorised tokens/network workarounds or fabricate success. A primary-repo PR alone does **not** prove two-repo Cloud support. A failure is a capability result, not permission to broaden access.

**Fallback:** One Codex Cloud task per repo with a shared external contract/test matrix, separate PRs, synchronized integration gates. Preferred even if both write-capable, for isolation and independent CI.

## 9. Durable execution register (update after every gate)

**Batches are owner-approved execution gates; C/P workstream rows below remain detailed planning references.**

| Batch | Status | Paired PRs / acceptance | Critical handoff |
|---|---|---|---|
| B1 | IN_PROGRESS | Backend [Draft PR #26](https://github.com/hassan-anari/WEDCAL/pull/26); frontend B1 PR pending | Backend PR_OPEN at `90bf6930a5036ea6cd720605ef4f1d6079153eda`; current-head CI evidence in PR #26. P-01/P-04 contract and regression delivery: [handoff](docs/B1_BACKEND_HANDOFF.md). Frontend/paired Telegram and cache verification pending; no batch acceptance or next-batch approval. |
| B2 | PLANNED | — | C-01 canonical Tap reconciliation, atomic backend leases, instant frontend multi-touch, legacy drain |
| B3 | PLANNED | — | Daily/Missions/Typing/Referral/Creator/Network |
| B4 | PLANNED | — | Profile/collection/notifications/Arena/DLX |
| B5 | PLANNED | — | Clan lifecycle/governance/Recovery/Treasury/Shop |
| B6 | PLANNED | — | Staging E2E, measured load, release gates/rollback |

| ID | Status | Owner approval/merged PRs | Evidence, blockers, next handoff |
|---|---|---|---|
| C-00 | ACCEPTED | Backend #22 merged | GitHub connector dual-repo proof in backend #24 / frontend #1; two Codex environments selected; PH-037 still owner action |
| C-01 | PLANNED | — | Update conflicting Tap authoritative contracts |
| P-01 | IN_PROGRESS (B1) | Backend [Draft #26](https://github.com/hassan-anari/WEDCAL/pull/26); frontend pending | Auth malformed-input/error regressions addressed; VPN root cause not reproduced. Frontend SDK/credential findings and real WebView gate in B1 handoff. |
| P-02 | PLANNED | — | Transactional uncapped Tap backend/migration |
| P-03 | PLANNED | — | Instant Tap frontend/end-to-end |
| P-04 | IN_PROGRESS (B1) | Backend [Draft #26](https://github.com/hassan-anari/WEDCAL/pull/26); frontend pending | Typed additive Bootstrap, config/feature/gameplay metadata and coherent progression/Season snapshot; frontend typed adapter and scoped cache gate pending. |
| P-05 | PLANNED | — | Daily/Missions |
| P-06 | PLANNED | — | Typing |
| P-07 | PLANNED | — | Referral/Creator/Network |
| P-08 | PLANNED | — | Profile/Collection/Notifications |
| P-09 | PLANNED | — | Arena rankings/off-chain DLX |
| P-10 | PLANNED | — | Clan core |
| P-11 | PLANNED | — | Clan governance/recovery/emergency exits |
| P-12 | PLANNED | — | Clan economics |
| P-13 | PLANNED | — | Security/E2E/observability/release |

**Update protocol:** A batch handoff must update its batch row and the relevant C/P sub-workstream rows, include URLs of BOTH related frontend/backend PRs where applicable, record commit SHAs and checks passing, note migration/rollout status, mark blockers, link owner acceptance, and set the next authorized batch. This ledger is not itself evidence of successful testing: link raw CI and benchmark artifacts.

## 10. Mandatory agent handoff format

Every future Codex or other agent's final batch summary and PR description must contain:

- **Batch / scope:** exact B1–B6 ID plus completed C/P workstream IDs; repo, branch, head SHA; changed paths; explicit out-of-scope work.
- **Contract decisions:** source documents read, discrepancies discovered, API request/response/error additions, versioning and owner decisions needed.
- **Implementation:** concise data-flow description including transaction/lock order, idempotency and frontend state ownership.
- **Test evidence:** commands executed, pass/fail counts, CI jobs/URLs, Postgres concurrency, E2E/Mini App recordings, load profiles and actual metrics.
- **Deployment:** old/new client compatibility, migrations, feature flag, staging/prod activation and rollback without replay/double credit.
- **Residual risk:** P0/P1/P2 with repro/proof and exact follow-up.
- **Handoff:** PR URL(s), unmerged status, current program register state, next scoped task and explicit STOP awaiting independent review/owner approval.

**Independent review contract:** Re-read the actual head diff and all required checks (do not trust agent summary alone). Block P0 (auth bypass, double rewards, loss of earned points, unauthorized Clan actions, broken live API, privacy/secrets) and P1 (wrong state transitions, stale balances/caches, rollback gaps, false VPN error). No phase is "complete" solely because an agent declares success.

## 11. Explicitly excluded / deferred work

No Season 1 Clan War/Defence/Rivalry/Alliance/Warfront engine, no Officer role or Captain succession, no arbitrary unilateral Clan kicking, no exchange-site account/whitelist integration, no wallet/on-chain transfers, no claimable/tradable/on-chain DLX, and no fabricated USD valuation. No gratuitous UI redesign, framework rewrite or broad refactor. These exclusions follow the approved S1 source of truth.

## 12. Open design reviews (must be closed before affected implementation)

| ID | Required decision or evidence | Blocking |
|---|---|---|
| D-01 | Final typed Tap lease API, session/device rules and valid event replay/expiry semantics | P-02/P-03 |
| D-02 | Exact handling of app loss before unsent tap intents reach server; guaranteed UX vs physical durability trade-off | P-03 |
| D-03 | Tap rate/abuse detection under unlimited lifetime/season earning, per-burst cap, DB throughput and economics inflation | P-02/P-13 |
| D-04 | Existing pending Tap economic obligation drain, source-time, receipt cleanup, close/epoch barrier and revert proof | P-02 |
| D-05 | VPN-off reproduced root cause and self-hosted SDK update/integrity/compatibility strategy | P-01 |
| D-06 | Frontend CI, required GitHub branch protection and safe automatic production deploy practice | P-13 / public release |
| D-07 | PH-043 read-season-brief owner decision | Public release |
| D-08 | Missing Clan target membership ID / join request/recovery/shop/effect read data for UX | P-10–P-12 |

---

**Owner-approved intent summary:** One stable Telegram Mini App, two independent production-grade codebases, a single auditable phased integration roadmap, instant server-authorized Tap feedback, transactionally committed and unbounded-across-days Tap earning with 50-per-burst/10-second cooldown, full multi-touch, Telegram VPN-off resilience, complete Season 1 feature parity and independent PR-based controls. Preserve all existing S1 exclusions. Do not implement later phases without the owner's explicit gate approval.
