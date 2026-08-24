# Dexo Land — Season 1 Product & Backend Source of Truth

**Status:** Approved execution baseline for Season 1 backend development  
**Language:** English  
**Primary audience:** Codex / AI coding agents, backend engineers, reviewers, QA, product owner  
**Frontend reference snapshot:** `Dalexio-Game` v`6.0.0` (final Mini App snapshot reviewed before this document)  
**Legacy backend reference:** `Dalexio-main.zip` Airdrop module, used only as a library of proven technical patterns  
**Last updated:** 2026-08-24

---

## 0. Purpose of this document

This document is the canonical Season 1 product/backend contract for Dexo Land.

It exists so that multiple engineers and multiple AI coding agents can work on the backend without re-interpreting old conversations, reviving removed features, inventing reward rules, or copying legacy Dalexio business logic that no longer matches the final Mini App.

The backend must implement the Season 1 product represented by the **final frontend**, plus the explicit backend/business decisions recorded in this document.

This is not a brainstorming document. It is an execution contract.

When a numeric value, threshold, percentage, duration, capacity, reward amount, level curve, anti-abuse threshold, or economy formula is not finalized, the mechanism must still be implemented, but the number must be represented as configurable data rather than embedded into domain logic.

The project must not be delayed because a tunable number is not finalized.

---

# 1. Authority and source hierarchy

## 1.1 Product-scope Source of Truth: the final Mini App frontend

The final Mini App source is the primary authority for:

- which Season 1 features exist,
- which screens and user-visible states exist,
- which roles exist,
- which flows are exposed to users,
- which systems are intentionally locked or marked for later seasons,
- which features were removed to reduce complexity,
- the current navigation and information architecture.

Examples:

- Main navigation is `Base / Earn / Clan / Arena / Profile`.
- Earn contains `Earn / Network / Typing`.
- Network contains Referral and Telegram Creator/Group functionality.
- Clan has only `Captain` and `Soldier`; there is no Officer role in Season 1.
- Soldier discovery contains Discover and Search; full rankings belong in Arena.
- Arena Season 1 contains Player Rankings and Clan Rankings plus a locked Season 2 Warfront preview.
- War/Defence/Rivalry implementation is not a Season 1 backend requirement.

Frontend fixture values are **not automatically economy contracts**. They are often sample data.

## 1.2 Explicit decisions recorded in this document

The frontend cannot express every backend requirement. The product owner has explicitly defined additional rules for:

- Telegram silent user creation,
- creator and speaker reward paths,
- anti-fraud behavior,
- mission verification,
- Typing claimable balance,
- Clan membership governance,
- inactivity handling,
- moderation and message evidence,
- Treasury splitting,
- Season/Lifetime point semantics,
- final DLX allocation timing.

Those approved backend/business rules are authoritative even when the frontend currently mocks a simpler state.

## 1.3 Old conversations

Earlier conversations are historical design context only.

They may explain intent, but they must not override the final frontend or this document.

Removed or superseded examples include:

- Officer/Vice-Captain as a Season 1 role,
- fixed Clan capacity such as 100 members,
- Level 20 as a hard Clan unlock requirement,
- War as a Season 1 backend feature,
- whitelist/account-linking requirements from the older project,
- old direct mutable airdrop score behavior.

## 1.4 Legacy Dalexio Airdrop backend

The old backend is **not** a business Source of Truth.

It is a source of proven technical implementation patterns that may be ported/refactored when they still fit the new architecture.

Allowed reuse is documented later in this file.

## 1.5 No invented business rules

An implementation agent must not resolve an unspecified economy choice by guessing.

If a mechanism is required but a number is not finalized, implement a configuration field and use development/test fixture values only where required for tests or local demos.

Do not hide product decisions inside constants.

---

# 2. Decision labels

This document uses the following labels conceptually.

### `S1-CONTRACT`
Required Season 1 behavior.

### `ARCH-CONTRACT`
Required architecture/infrastructure behavior.

### `CONFIG`
A tunable value or formula. The mechanism is required; the exact value may change without a code rewrite.

### `FUTURE`
Explicitly outside Season 1 product behavior, though generic infrastructure may remain extensible.

### `NON-BLOCKING`
Not finalized, but backend development must continue because the uncertainty can be isolated behind configuration or an interface.

No unresolved item in this document is intended to block Phase 1 foundation development.

---

# 3. Product definition

Dexo Land Season 1 is an independent Telegram game/airdrop service built around:

- Telegram identity,
- persistent player progression,
- seasonal competition,
- daily rewards and missions,
- a permanent Typing mini-game,
- referrals,
- a Telegram creator/community growth program,
- player identity, achievements, medals and collection,
- a seasonal Captain/Soldier Clan system,
- Clan missions, contribution, Treasury and shared inventory,
- Player and Clan rankings.

The game uses Telegram as both the acquisition surface and a real gameplay/activity network.

Dexo Land is **not** a Django app/domain inside the older exchange/site project. It is a separate project, separate deployment, separate database and separate codebase.

There is one Telegram bot for Dexo Land.

There is no Season 1 whitelist requirement.

Users do not need an existing Dalexio exchange account to participate.

---

# 4. Season 1 scope

## 4.1 In scope

1. Foundation / onboarding
2. Telegram identity and authentication
3. Silent player creation
4. Persistent player progression and Dexo progression
5. Season model and current-season state
6. Base/home bootstrap data
7. Daily reward
8. Missions
9. Mission verification framework
10. Referral system
11. Creator / Telegram Group Program
12. Telegram activity ingestion
13. Rule-based anti-fraud and player flagging
14. Typing Arena
15. Typing claimable reward wallet
16. Profile summary
17. Achievements
18. Medals
19. Frames / titles / collection
20. Clan unlock and seasonal role selection
21. Captain Telegram-group-based Clan registration
22. Soldier discovery/search/join requests
23. Telegram invite delivery and membership verification
24. Clan membership lifecycle and Telegram access synchronization
25. Clan inactivity handling
26. Voluntary Clan leave with friction/cooldown
27. Captain member-removal request and governance/moderation
28. Clan recovery/dissolution governance for inactive Captain
29. Clan HQ / progression / capacity
30. Clan missions
31. Clan contribution
32. Clan Treasury
33. Clan Shop and stackable Inventory
34. Generic item/effect infrastructure
35. Clan achievements / Trophy Hall data
36. Player rankings
37. Clan rankings
38. Notifications
39. Telegram DMs for selected events
40. Broadcast/admin messaging
41. Game configuration/admin controls
42. Season close/archive behavior
43. Production hardening, observability, backups and load testing

## 4.2 Explicitly outside Season 1

The following are **not** Season 1 backend features:

- Clan War engine
- War matchmaking
- Defence engine
- Rivalry engine
- Alliance engine
- World Events engine
- War Vault
- attack/defence item effects
- battle histories
- Season 2 Warfront business logic
- wallet connection
- on-chain withdrawal
- DLX distribution during each season
- Dalexio exchange account linking
- whitelist flows
- Officer role
- Captain succession system

The Season 1 Arena may expose a locked Warfront/Season 2 preview exactly as the frontend does, but that does not authorize War backend implementation.

---

# 5. Core domain invariants

These invariants must remain true unless this document is explicitly revised.

1. PostgreSQL is the durable Source of Truth.
2. Redis is disposable infrastructure, never the sole economic Source of Truth.
3. Telegram webhook is ingress, not the scoring engine.
4. Every reward-producing mutation must be idempotent and auditable.
5. A reward event is never applied twice because of retries.
6. User-facing numeric economy values must come from configuration, not scattered constants.
7. A Telegram Group membership and a Dexo Land Clan membership are different concepts.
8. A Telegram invite link is a disposable access credential, not membership state.
9. Clan Contribution never moves from one historical Clan membership period to another.
10. A Captain cannot bypass Dexo Land governance by manually banning a Soldier in Telegram.
11. War-related future frontend code/diagnostic states must not create Season 1 backend scope.
12. Season points and lifetime points are two projections of the same point events, not two independently minted currencies.
13. Final DLX allocation happens only after the overall multi-season campaign ends, not after each individual season.

---

# 6. Backend architecture

## 6.1 Stack

Target stack:

- Python
- Django
- Django Ninja
- PostgreSQL
- Redis
- RabbitMQ
- Celery
- Docker / Docker Compose
- pytest
- GitHub Actions

The project starts as a **modular monolith**, not microservices.

Expected domain modules may include:

```text
apps/
  identity/
  telegram/
  players/
  seasons/
  progression/
  rewards/
  missions/
  typing_game/
  referrals/
  network/
  moderation/
  clans/
  inventory/
  achievements/
  rankings/
  notifications/
  adminops/
```

Exact Django app naming can be adjusted during Phase 1, but domain boundaries must stay clear.

## 6.2 Docker topology

Development Compose should include at minimum:

```text
api
worker
beat
postgres
redis
rabbitmq
```

The API, worker and beat services should use the same application image with different commands.

The containerized project must support reproducible local development and CI.

## 6.3 Horizontal scale assumptions

The target may reach 500k+ registered users and potentially more.

The architecture must not assume 500k concurrent HTTP connections; the main burst risk is Telegram event ingestion and leaderboard/read traffic.

Stateless API processes should be horizontally scalable.

Long-running or bursty work belongs in queues/workers.

---

# 7. Telegram identity and authentication

## 7.1 Single bot

There is one Dexo Land Telegram bot.

Do not add multi-bot domain complexity.

## 7.2 Three valid first-seen/user-creation paths

A Dexo Land player can first enter the system through any of these paths:

1. private `/start` with the bot,
2. Mini App authentication using Telegram `initData`,
3. sending a message inside a monitored Telegram Group where the bot receives the event.

Therefore `/start` is not a prerequisite for account creation.

When a previously unseen Telegram user appears in a group message, create a minimal Player identity record keyed by Telegram user ID.

Later `/start` or Mini App authentication enriches the same record. It must not create a duplicate player.

Recommended fields include:

- telegram_user_id, unique
- first_name nullable
- last_name nullable
- username nullable
- photo reference nullable
- language_code nullable
- first_seen_at
- first_seen_source: `bot_start | mini_app | group_activity`
- first_private_contact_at nullable
- first_mini_app_at nullable
- last_seen_at

## 7.3 Mini App authentication

The backend must validate Telegram Mini App `initData` server-side using Telegram's documented HMAC validation flow.

Never trust a Telegram user ID sent as a plain client field.

Authentication should resolve or create the canonical Player record and refresh safe profile metadata.

## 7.4 Bot `/start`

`/start` resolves the player and parses referral attribution when present.

First-touch referral attribution must be idempotent and immutable according to the Referral section.

---

# 8. Telegram webhook and ingestion

## 8.1 Webhook security

The webhook must verify Telegram's configured secret token.

The legacy backend's secret-verification pattern may be reused/refactored.

## 8.2 Fast ingress path

Required shape:

```text
Telegram Update
→ verify webhook secret
→ dedupe update_id
→ normalize minimum event
→ durably enqueue/persist
→ ACK Telegram quickly
→ async worker processing
```

Scoring, referral payouts, Clan contribution and expensive Telegram API checks must not execute synchronously inside the webhook request.

## 8.3 Dedupe and idempotency

At minimum:

- dedupe Telegram `update_id`,
- dedupe message/activity identity,
- dedupe reward settlement source keys,
- ensure retrying Celery work cannot double-credit a user or Treasury.

## 8.4 Normalized events

Domain services should consume normalized event objects rather than raw Telegram JSON.

Examples:

- `TelegramUserSeen`
- `TelegramMessageObserved`
- `BotMembershipChanged`
- `UserMembershipChanged`
- `GroupMetadataUpdated`
- `BotStarted`

This isolates the game rules from Telegram payload details.

---

# 9. Message evidence, privacy and moderation retention

The earlier plan to never retain message text is no longer sufficient because Dexo Land needs to investigate reports such as harassment, sabotage, spam or abusive behavior inside Clan groups.

## 9.1 Message logging

For monitored/reward-relevant groups, backend may store a moderation-oriented message log containing fields such as:

- Telegram chat ID
- Telegram message ID
- sender/player ID
- sent timestamp
- text/content where applicable
- character count
- normalized fingerprint/hash
- activity context (`NETWORK_GROUP` or `CLAN_HOME`)
- automated classification
- fraud/risk flags
- deletion/edit metadata when observed

## 9.2 Retention

Full message content must have a configurable retention window.

It must not be retained forever by default.

Messages attached to an active Moderation Case may be preserved beyond the normal retention window until the case and required audit period are complete.

## 9.3 Access control

Message content is sensitive operational data.

Admin/support access must be permission-controlled and auditable.

Do not expose raw message logs in normal player APIs.

---

# 10. Canonical points, season score, lifetime score and level

This is a critical contract.

## 10.1 One canonical point event

The game does not mint one independent value for Season Score and another independent value for Lifetime Score.

When an eligible reward event grants `X` player points:

```text
Reward Event +X
     ├─ Current Season Points +X
     └─ Lifetime Points +X
```

This is one event, projected in two scopes.

## 10.2 Lifetime Points

Lifetime Points are conceptually:

```text
sum(all eligible point events from all closed seasons)
+ sum(point events in the active season)
```

They must not be incremented again during season settlement, otherwise scores would double-count.

## 10.3 Season Points

Season Points are the sum of canonical point events whose `season_id` equals the active season.

Season Points power seasonal ranking and season-specific presentation.

## 10.4 Frontend XP terminology

The final frontend currently uses terminology such as `XP`, `lifetimeXp`, reward XP and level progress.

For backend consistency, the canonical reward unit should be one player point/XP ledger amount, with:

- lifetime projection,
- season projection,
- level progression derived from lifetime/progression totals according to a configurable level curve.

Do not create parallel reward currencies merely because different frontend components use different labels.

## 10.5 Level

Player Level is persistent progression.

Level thresholds and curves are `CONFIG`.

The current frontend's values are sample data, not hard contracts.

## 10.6 Dexo stages/evolution

Dexo progression/evolution is derived from player progression according to configurable thresholds.

The backend should expose the current Dexo stage/state required by the frontend.

Exact level thresholds are configurable.

---

# 11. Reward Ledger

## 11.1 Ledger-first accounting

Never implement economic logic as only:

```python
player.points += x
```

The authoritative mutation must produce a durable reward/accounting record.

A simplified reward ledger concept:

```text
RewardLedger
- id
- player_id
- season_id
- source_type
- source_id
- context
- gross_amount
- player_amount
- clan_treasury_amount
- referral_amount / related metadata where applicable
- config_version
- idempotency_key UNIQUE
- metadata
- created_at
```

Not every field must exist literally on one table if a cleaner normalized design is chosen, but the audit semantics are required.

## 11.2 Idempotency

Every reward settlement must have a deterministic idempotency key derived from its source event/action.

Examples:

- daily claim for player/date,
- mission claim for player/mission/version,
- typing wallet claim transaction,
- creator activity aggregate settlement window,
- referral share generated from a source reward event,
- Clan mission reward,
- Treasury split from a Clan-context point event.

## 11.3 Config version recording

When a reward depends on configurable economy rules, record the effective GameConfig/economy version or enough rule metadata to later audit the calculation.

---

# 12. Versioned Game Configuration

Every tunable value must be centrally configurable and, where needed for auditability, versioned by season/effective time.

Recommended namespaces include:

```text
progression.*
daily.*
missions.*
typing.*
referral.*
network_activity.*
clan_activity.*
anti_fraud.network.*
anti_fraud.clan.*
clan.unlock.*
clan.capacity.*
clan.inactivity.*
clan.governance.*
clan.treasury.*
clan.items.*
rankings.*
notifications.*
moderation.*
```

Network Group activity and Clan Home activity must use **separate economy/anti-fraud policies**, even when they use the same engine.

Development fixtures may mirror current frontend demo values, but those values must not become irreversible domain constants.

---

# 13. Bootstrap and Base

The Mini App needs a compact bootstrap endpoint or equivalent initial data contract.

It should provide enough data to render the shell and current player state without many serial requests.

Candidate bootstrap data:

- authenticated player profile,
- level/progression,
- lifetime points,
- current season and season points,
- streak/daily state,
- Dexo state,
- active feature flags,
- Clan eligibility/role/membership summary,
- unread notification count,
- essential config-derived UI flags.

Base is the player home surface.

The existing sample War alert in Base is not a Season 1 backend requirement. Do not generate real War alerts in Season 1.

---

# 14. Daily reward

Daily reward is part of Season 1.

The backend must own:

- eligibility window,
- last claim,
- streak behavior,
- claim idempotency,
- reward settlement,
- history.

Reward amount and reset boundary are `CONFIG`.

Daily claim must be concurrency-safe; simultaneous requests must not grant twice.

Use a database uniqueness constraint and/or row-level locking/atomic settlement, not only Redis locks.

---

# 15. Missions

## 15.1 Mission model

Mission definitions are admin/config driven.

The frontend includes categories/actions such as ecosystem/social/skill/profile/season and internal/external/typing/profile-like actions.

Backend should model a reusable mission definition, availability, progress state and reward claim.

## 15.2 Canonical mission state

Recommended logical lifecycle:

```text
AVAILABLE
→ IN_PROGRESS
→ VERIFIED / REWARD_READY
→ CLAIMED / COMPLETED
```

Exact naming may differ, but verification and reward settlement must be separate enough to remain idempotent.

## 15.3 Mission verification — three Season 1 strategies

Season 1 has exactly three required verification families:

### A. `INTERNAL`

The backend already owns the relevant state.

Examples:

- complete a Typing run,
- reach a profile state,
- complete another in-app action.

No external proof is needed.

### B. `TELEGRAM`

The bot/backend verifies Telegram state.

Primary example:

- joining a required Telegram group/community.

The backend checks the actual membership state via Telegram/bot data. The client cannot self-assert completion.

### C. `TIMED_EXTERNAL`

Used for opening a website, X page or other external destination where Season 1 intentionally uses an UX-first verification approach instead of OAuth/provider integration.

Flow:

```text
user starts external mission
→ backend records/open-token with opened_at
→ client opens external destination
→ configurable minimum dwell time passes
→ user later resumes/rechecks Mini App
→ backend verifies elapsed time/token state
→ mission becomes reward-ready
```

The backend must not trust a client-side timer as authoritative.

A value such as 6 seconds is a `CONFIG` sample, not a permanent contract.

## 15.4 Mission reward claim

Verification does not itself need to mutate points if keeping a separate reward-ready/claim step matches the frontend.

Claim must be idempotent.

---

# 16. Referral system

## 16.1 Referral link

The product uses Telegram start links containing a per-player referral identifier/code.

Do not expose internal database IDs if an opaque/public code is preferable.

## 16.2 First-touch attribution

First valid referral attribution wins.

Once an invitee has a valid inviter relation, later `/start` parameters must not silently replace it.

The legacy implementation's first-touch concept can be reused/refactored.

## 16.3 Pending to verified

A referred user is not necessarily successful immediately.

Verification/activation conditions are configurable and may use a progression threshold such as the current frontend's sample Level 5 requirement.

## 16.4 Referral tiers

Bronze/Silver/Gold presentation exists in the frontend.

Thresholds and percentages are `CONFIG`.

Do not hardcode frontend samples into domain services.

## 16.5 Passive referral reward

The infrastructure must support a configurable percentage/share from eligible invitee reward events after the referral becomes verified.

Which source event types are referral-share-eligible is configuration.

The referral share must be generated from the source reward event idempotently so retries cannot duplicate passive reward.

## 16.6 Anti-Sybil

Referral verification must not rely only on account creation.

Eligibility should use real progression/activity criteria configured for the season.

Suspended/banned/fraudulent accounts must not generate valid referral rewards.

---

# 17. Network — Creator / Telegram Group Program

## 17.1 Purpose

This is the second permanent Network path alongside Referral.

A creator/community owner brings the Dexo Land bot into a Telegram Group/Supergroup and makes the bot Admin.

Qualified activity by community members can then generate creator rewards.

## 17.2 Group registration flow

```text
Creator initiates Add Bot to Group
→ Telegram startgroup/add flow
→ bot joins group
→ bot is promoted to Admin
→ backend observes/verifies bot membership/admin state
→ backend associates eligible creator with group registration
→ group enters review/connected state
→ after qualification it becomes ACTIVE
→ verified member activity can generate configured creator rewards
```

The user never types a Telegram Chat ID into the Mini App.

## 17.3 Bot Admin requirement

A reward-producing Creator Group must have the Dexo Land bot as Admin with the permissions required by the Season 1 integration.

If bot admin status is lost:

- reward generation pauses,
- group becomes `ADMIN_REQUIRED` / suspended-equivalent,
- creator is notified,
- activity may still be logged for safety/diagnostics but is not settled as qualified creator reward until restored according to policy.

## 17.4 Creator ownership

Each active group registration has one creator reward owner.

Other Telegram admins are not automatically co-owners of Dexo Land creator rewards.

Creator ownership changes require explicit backend re-verification.

## 17.5 Multiple groups

A creator can register multiple groups, subject to configurable limits/qualification rules.

## 17.6 Creator messages

The creator's own messages must not be a trivial self-farming path.

The safe Season 1 default is:

- creator reward is primarily derived from qualified activity by **other** valid community members,
- creator self-activity can be excluded from creator reward unless/configured otherwise.

The engine must make this configurable.

## 17.7 Speaker personal reward path

Every speaker/player activity is processed by the activity engine and the architecture must support a personal reward for the speaker.

However the **initial Season 1 configuration keeps this Network Speaker Reward disabled / zero**.

Conceptually:

```text
network_activity.speaker_reward.enabled = false
network_activity.speaker_reward.amount/rates = 0
```

The capability must already exist so a future config change can activate personal speaker rewards without a schema/business-logic rewrite.

This also means a user first seen only through a group message still receives a Player record even while the personal reward amount is zero.

## 17.8 Group states

Recommended states:

- `REVIEW`
- `ACTIVE`
- `ADMIN_REQUIRED`
- `SUSPENDED`
- `INACTIVE`

Exact enum naming may vary.

---

# 18. Shared Telegram Activity Engine

Network Groups and Clan Home Groups use the same core activity engine but different configuration namespaces.

## 18.1 Contexts

At minimum:

```text
NETWORK_GROUP
CLAN_HOME
```

The same observed Telegram message may only be settled according to the context that actually owns that group registration at that time.

## 18.2 No daily activity cap

There is no general player daily cap for conversation activity in the current product decision.

Instead abuse prevention uses rate, content and behavioral rules.

## 18.3 Configurable qualification examples

Examples of tunable controls:

- maximum rewardable messages per minute (example: 5/minute)
- maximum rewardable characters per message (example: 200)
- minimum meaningful length
- duplicate/repeated content rules
- near-duplicate thresholds
- forwarded/copied content policy
- bot/service-message exclusion
- suspicious burst detection
- same-text repetition
- excessive low-information messages
- group diversity/unique-member weighting if desired
- active-member definition
- excluded users/states

The examples 5 messages/minute and 200 characters are not hardcoded final values.

## 18.4 Context-specific economy

`NETWORK_GROUP` and `CLAN_HOME` must have separate reward configuration.

For example, an eligible message may be worth a different amount in a Clan Home than in a normal Creator Group.

Never implement one global `points_per_character` constant for all contexts.

---

# 19. Anti-fraud and activity classification

Anti-fraud is a real Season 1 subsystem, not a single rate-limit check.

## 19.1 Activity classifications

Each reward-relevant activity can end in a classification such as:

- `VALID`
- `REJECTED`
- `SUSPICIOUS`
- `FLAGGED`

The exact data model can separate eligibility from risk severity, but equivalent semantics are required.

## 19.2 Signals

Rule-based signals can include:

- exact duplicate text,
- repeated near-duplicate text,
- message bursts,
- abnormal character patterns,
- extremely repetitive low-information content,
- impossible or bot-like timing,
- abnormal cross-group behavior,
- banned/suspended account activity,
- evasion patterns,
- manipulation attempts around mission/referral/clan rewards.

## 19.3 Player risk/account states

At minimum, the platform needs states equivalent to:

- `ACTIVE`
- `FLAGGED`
- `SUSPENDED`
- `BANNED`

Flagging is not the same as banning.

A flag should preserve evidence and allow review.

## 19.4 Config separation

Fraud thresholds/policies may differ between Network activity and Clan Home activity.

Use separate configuration namespaces even when the code engine is shared.

## 19.5 Reward behavior

Rejected activity grants no reward.

Suspicious/flagged activity may be withheld, rejected or routed for review according to configuration/policy.

Never credit and later silently mutate history without an auditable adjustment record.

---

# 20. Typing Arena

## 20.1 Permanent Season 1 mini-game

Typing is a permanent Earn subfeature.

Backend owns attempts, result submission, anti-cheat validation, reward accrual and claims.

## 20.2 Daily attempts

Current product direction is five completed attempts per day.

Treat `5` as a configurable default rather than embedding it in business logic.

Reset boundary/timezone is configuration.

## 20.3 Variable reward per attempt

Typing no longer uses one fixed reward per run.

Each completed/eligible attempt earns a variable amount based on at least:

- accuracy,
- typing speed/WPM.

The exact formula and thresholds are `CONFIG`.

Conceptually:

```text
attempt result
→ server validation
→ typing reward formula(accuracy, speed, config)
→ credit Typing Claimable Balance
```

## 20.4 Claimable Typing Balance

Typing rewards first accumulate in a claimable balance/wallet instead of immediately becoming canonical player points.

After any eligible attempt, the balance can increase.

The player may:

- claim after one attempt,
- perform several attempts and claim their accumulated balance later,
- carry unclaimed balance until a later claim according to normal account rules.

There is no requirement to finish all daily attempts before claiming.

## 20.5 Claim transaction

Claiming transfers the current claimable amount into the canonical Player point ledger and reduces the claimable Typing balance accordingly.

The operation must be atomic and idempotent.

Recommended model:

- `TypingAttempt`
- `TypingWallet` or equivalent balance projection
- `TypingWalletTransaction` credit/debit ledger
- canonical `RewardLedger` event when claimed

Do not erase attempts when claiming.

## 20.6 Anti-cheat

Frontend paste/copy prevention is only a UX deterrent.

Backend should validate plausible timing/result data and flag obviously impossible or repeated submissions.

Machine learning is not required in Season 1.

---

# 21. Profile, achievements and collection

## 21.1 Profile

Backend should support the final Profile tabs:

```text
OVERVIEW | ACHIEVEMENTS | COLLECTION
```

Profile is player identity/progression, not a settings dashboard.

Wallet and Dalexio Account integrations remain Coming Soon / Future.

## 21.2 Achievement engine

Achievement progress and unlock state are server-owned.

Exact reward amounts/thresholds are configurable.

## 21.3 Season 1 achievements

Season 1 includes at least the concepts already present in the frontend plus the clarified Clan achievements.

Current non-War concepts include:

- First Mission
- First Referral
- Seven Day Streak
- Thirty Day Streak
- First Verified Activity
- Clan Ready
- First Clan
- First Contribution

### Clan Ready

Season 1 achievement. Unlock when the configured Clan-readiness/unlock requirement is satisfied.

### First Clan

Season 1 achievement. Unlock when the player's first real Clan membership is activated.

### First Contribution

Season 1 achievement. The old frontend copy tied this to Defence/War, but the approved Season 1 meaning is the player's **first valid Clan contribution** in the active Clan system.

Do not wait for War.

## 21.4 Future achievements

The following remain Season 2/Future:

- First War
- First Victory

No War achievement evaluation is implemented in Season 1.

## 21.5 Frames, titles and medals

Collection owns/unlocks/equips frames, titles and medals.

Current frontend catalog concepts should be supported without turning sample numeric rules into hardcoded economy.

Season medals are awarded during season close according to final ranking/config rules.

---

# 22. Clan — Season 1 product definition

Clan is a full Season 1 system.

It is a game/social identity attached to a verified Telegram Home Base group.

The Telegram Group is the communication surface. The Dexo Land Clan is the game entity.

These are related but not identical.

## 22.1 Seasonal roles

Exactly two user roles exist:

- `CAPTAIN`
- `SOLDIER`

There is no Officer role in Season 1.

## 22.2 Clan unlock

The frontend currently unlocks Clan at Level 5.

Treat this as a configurable Season 1 default/gate, not a hardcoded forever threshold.

## 22.3 Seasonal role choice

A player chooses Captain or Soldier for a season.

Normal user-facing role switching is not offered.

Controlled recovery/exit mechanisms described below do not create an unrestricted role-switch UI.

## 22.4 One active Clan membership

A player cannot be an active member of multiple Clans at the same time.

There is no direct `Clan A → Clan B` switch action.

Leaving and joining another Clan must pass through the controlled leave/cooldown/application flow.

---

# 23. Persistent Clan identity and seasonal Clan state

To support multiple seasons without recreating every social identity, use this logical separation:

### `Clan`
Persistent identity tied to its Telegram group/community.

Examples:

- name/tag/motto
- Telegram group link/identity
- historical achievements
- creation metadata

### `ClanSeason`
Season-specific competitive/progression state.

Examples:

- HQ level/progress
- capacity
- Treasury
- Clan score
- active effects
- season achievements
- active roster/memberships

At season close, `ClanSeason` is archived.

The persistent `Clan` identity can participate in the next season after required revalidation/activation.

Seasonal roster and role state do not need to carry forward automatically.

This preserves the social identity while keeping seasonal competition fair.

---

# 24. Captain Clan registration

## 24.1 Eligibility

The actual final frontend does not include Clan Tickets, deposits or Officer requirements.

Season 1 Captain eligibility is based on:

- Clan unlock/progression gate,
- choosing Captain,
- having an eligible Telegram Group,
- backend verification of user authority and bot Admin status.

Additional economic qualification must not be invented unless later added to config/product contract.

## 24.2 Eligible Telegram groups

Backend lists groups it already knows and can verify.

A group is eligible when:

- the Dexo Land bot is Admin,
- the Captain is a valid owner/admin according to the verification policy,
- the group is not already improperly bound to another active Clan registration,
- required Telegram metadata is available.

## 24.3 No manual Chat ID

The user never enters a Telegram Chat ID manually.

Chat ID is backend-managed metadata from Telegram events/API.

## 24.4 Registration fields

User-facing Clan identity fields can include:

- Clan name
- short tag
- motto/short description
- accent/identity options supported by frontend
- image/group photo fallback behavior

Telegram metadata is not manually typed by the user.

---

# 25. Soldier discovery, request and acceptance

## 25.1 Discovery/Search

Soldier can browse Discover and Search.

Full canonical Clan ranking is not duplicated here; it lives in Arena.

## 25.2 Join request

A Soldier sends a join request to a Clan with available eligibility/capacity.

Backend protects against duplicate/concurrent requests.

A player should not have multiple accepted/active Clan memberships.

## 25.3 Captain review

Captain can see useful applicant data such as:

- Telegram identity/avatar fallback
- level
- season points
- global rank
- activity signal
- medals/achievements relevant to application review

The backend decides which fields are safe/public to expose.

## 25.4 Acceptance creates an invite, not final membership

Approved flow:

```text
Soldier applies
→ Captain accepts
→ backend creates user-specific invite state
→ bot sends Soldier a private DM: request accepted + private invite link
→ Soldier joins the Telegram Home Base
→ bot/backend verifies actual membership
→ Dexo Land ClanMembership becomes ACTIVE
```

Acceptance alone does not mean Telegram membership exists.

## 25.5 Invite links

Invite links are disposable credentials.

Recommended invite record:

```text
ClanInvite
- clan_id
- user_id
- telegram_invite_ref/link metadata
- status: PENDING | USED | EXPIRED | REVOKED
- created_at
- expires_at
- used_at
```

If a link expires while the player is still eligible, backend may generate a new one.

Invite expiration must never delete or corrupt a real Clan membership.

---

# 26. Clan membership vs Telegram group membership

This separation is mandatory.

```text
Dexo Land ClanMembership
        !=
Telegram Group Membership
        !=
Telegram Invite Link
```

## 26.1 Normal active state

A healthy Soldier membership usually has:

- active Dexo Land ClanMembership,
- confirmed Telegram Home Base membership,
- current season context.

## 26.2 Telegram access lost

If an active Clan member leaves or is removed from the Telegram Group, do **not** immediately delete the Dexo Land Clan membership.

Set an access state equivalent to:

```text
ACTIVE → ACCESS_LOST
```

Pause new Home Base activity contribution until access is restored.

Notify the player and provide a rejoin/recovery path when allowed.

## 26.3 Accidental/self leave

If a Soldier leaves Telegram accidentally:

```text
Telegram leave observed
→ ClanMembership ACCESS_LOST
→ bot/in-app notification
→ regenerate valid invite if eligible
→ player rejoins
→ verify membership
→ return to ACTIVE
```

An access-loss grace period is configurable.

## 26.4 Membership event log

All meaningful state changes must create an audit event, e.g.:

- requested
- accepted
- invite created
- Telegram joined
- activated
- access lost
- rejoined
- voluntary leave
- removal requested
- removed
- inactivity removal
- Clan disband release

---

# 27. Voluntary Clan leave

Players are committed to a Clan but are not prisoners.

A Soldier may leave voluntarily, with deliberate friction.

## 27.1 Flow

```text
ACTIVE
→ player requests Leave Clan
→ clear consequences/confirmation
→ membership closed with VOLUNTARY_LEAVE reason
→ join cooldown begins
→ after cooldown, player may apply to another Clan
```

There is no instant direct switch.

## 27.2 Cooldown / limits

Cooldown duration and optional max voluntary leaves per season are `CONFIG`.

The backend mechanism must support them from the start.

## 27.3 Historical Contribution

Contribution already earned for the old Clan remains attached to that Clan membership period.

It does not migrate to the new Clan.

Example:

```text
Nova contribution: 5,000
player leaves
Titan contribution later: 2,000
```

History remains `Nova=5,000`, `Titan=2,000`.

---

# 28. Captain member removal, Telegram ban bypass and governance

A Captain must not have an unrestricted backend `kick immediately` power.

## 28.1 Removal request

Captain initiates a `ClanRemovalCase` and provides a reason category, e.g.:

- abusive behavior
- spam
- harassment
- sabotage
- inactivity
- other community reason

## 28.2 Two removal paths

### Community/Governance removal

For ordinary Clan disputes where the player is not violating platform-wide safety rules:

```text
Captain proposes removal
→ timed vote opens
→ eligible Clan members vote
→ quorum/approval threshold evaluated
→ approved: membership closed
→ rejected/expired: membership remains
```

Quorum, voting duration and approval threshold are `CONFIG`.

The target member does not vote on their own removal.

The system must define eligible voters deterministically (for example active current roster members excluding target); exact thresholds remain config.

### Moderation removal

For serious platform violations such as fraud, harassment, scams, automated abuse or severe spam:

```text
report/evidence
→ Dexo Land Moderation Case
→ moderator/admin action
→ dismiss / warn / flag / suspend / ban / remove-from-clan
```

A serious moderation action does not need to wait for Clan voting.

## 28.3 Captain manually bans/removes Soldier in Telegram

A Captain may try to bypass Dexo Land by banning a user directly in Telegram.

Backend behavior:

```text
Telegram ban/removal observed
→ mark ClanMembership ACCESS_LOST
→ create Membership Incident
→ DO NOT automatically treat it as an approved Dexo Land removal
```

If there is an already-approved removal/moderation case, finish the removal workflow.

If there is no approved case:

- record unauthorized membership change incident,
- notify Soldier,
- require Captain to provide/attach a valid reason/case,
- route to governance or moderation,
- restore/reissue access when the removal is not approved and Telegram permissions allow it.

A Captain cannot defeat game governance solely through Telegram admin controls.

## 28.4 Captain abuse tracking

Repeated unapproved bans, failed removal cases or validated reports against a Captain can generate Captain/player risk flags and moderation review.

Captain status does not exempt a player from platform moderation.

---

# 29. Clan inactivity system

Inactivity is a required Season 1 mechanism.

Thresholds such as 3 days and 5 days are examples and remain configurable.

## 29.1 What counts as Clan activity

Use a server-owned `last_clan_active_at` or equivalent based on meaningful signals, such as:

- qualified Clan Home activity,
- Clan mission participation,
- eligible contribution-generating actions,
- meaningful authenticated Clan interaction defined by policy.

Do not mark an active Clan participant inactive merely because they did not open one specific screen.

## 29.2 Soldier inactivity

Conceptual flow:

```text
ACTIVE
→ warning threshold reached
→ mark inactive-warning
→ bot DM + in-app notification asking player to return
→ critical threshold reached
→ membership becomes inactivity-removal eligible
→ system closes membership with INACTIVITY reason
```

An inactivity removal is not the same as misconduct.

It should not create a moderation penalty by itself.

## 29.3 Captain inactivity

Do not automatically destroy a Clan the instant a Captain crosses a threshold.

Conceptual flow:

```text
Captain active
→ warning threshold
→ bot reminders
→ critical threshold
→ CLAN RECOVERY MODE
→ timed dissolution/recovery governance vote
```

During Recovery Mode:

- member history and Treasury remain safe,
- normal existing member activity may continue where possible,
- Captain-only high-risk actions/spending/recruitment can be restricted,
- members are informed,
- governance can decide whether the abandoned Clan should be dissolved and members released.

## 29.4 Recovery vote

Season 1 does not implement Captain succession.

The simple Season 1 recovery outcome is dissolution/release.

If the recovery vote is approved:

- ClanSeason is closed/disbanded according to policy,
- members are released without ordinary voluntary-leave penalty/cooldown,
- history/contribution remains archived,
- members may apply elsewhere.

If the Captain returns before a recovery vote is formally opened, recovery can be cancelled automatically after valid activity is confirmed.

Once a vote is open, it should follow deterministic governance rules rather than disappearing unpredictably.

## 29.5 Captain voluntary dissolution

If a Clan has only the Captain, Captain may dissolve directly.

If other members exist, Captain dissolution becomes a timed member vote/proposal so one person cannot instantly destroy the team's season state.

Exact voting rules are configurable.

---

# 30. Clan Group failure / bot admin loss

If the Home Base group is deleted, inaccessible, or the bot loses required Admin status:

- Clan enters a recovery/admin-required state,
- reward-producing Clan Home activity pauses,
- Captain and members are notified,
- a configurable grace/recovery window begins,
- if restored, normal state resumes,
- if not recoverable, governance/admin recovery may release members and close the ClanSeason.

Do not trap Soldiers in an unrecoverable Clan.

---

# 31. Clan HQ, level and capacity

Clan HQ is a Season 1 progression system.

## 31.1 HQ states

Frontend supports concepts equivalent to:

- locked
- normal
- upgrade available
- recovery mode
- max level

Backend should expose state derived from actual game data, not rotate states as decoration.

## 31.2 Capacity grows with progression

Clan capacity is not a fixed global number.

The Clan starts with a configurable lower capacity and can unlock more capacity through HQ/Clan progression.

Capacity curve, maximum and upgrade costs/requirements are `CONFIG`.

## 31.3 No hardcoded demo levels

Frontend demo Clan levels/capacities are examples.

Do not encode those values as universal rules.

---

# 32. Clan Missions

Season 1 supports:

- group Clan missions,
- individual Clan missions.

Mission definitions, windows, requirements and rewards are configuration-driven.

Clan mission activity can generate:

- Player points,
- Clan Contribution,
- Clan XP/progression,
- Treasury share/reward,

according to the configured reward matrix.

All outputs must be auditable and idempotent.

---

# 33. Clan Contribution

## 33.1 Captain is included

Captain is a roster member for contribution accounting.

Captain earns normal Contribution through eligible actions just like Soldiers.

## 33.2 Contribution meaning

Contribution measures how much eligible value a player generated for that Clan during a membership period/season.

## 33.3 Gross-before-Treasury contribution

When a Clan activity reward is split between Player and Treasury, Contribution should be based on the **gross eligible activity value before Treasury split**.

Example:

```text
Gross eligible Clan activity = 10
Treasury split = 10%
Player receives = 9 points
Treasury receives = 1 credit
Clan Contribution credited to player = 10 contribution units (subject to config/rule mapping)
```

This preserves Contribution as a measure of work generated for the Clan rather than only the net personal payout.

Exact mapping between gross reward units and contribution units remains configurable.

## 33.4 End-of-season participation percentage

At season close, Clan member participation can be represented as:

```text
member season contribution / total Clan season contribution
```

This percentage is used later for Clan reward distribution logic when the campaign eventually settles DLX allocation or other configured Clan rewards.

## 33.5 Captain bonus

Captain receives an additional configurable leadership bonus because they carried the Captain role.

The exact percentage/formula is an Economy Contract item.

Implementation must support the bonus without hardcoding its value.

The preferred accounting model is to calculate leadership bonus explicitly during allocation/reward distribution rather than artificially inflating historical Contribution.

---

# 34. Clan Treasury

Treasury is a shared Season 1 Clan resource used for Clan purchases/actions.

It is not directly withdrawable as DLX.

## 34.1 Treasury ledger

Treasury must have its own append/audit ledger for credits and debits.

Never rely only on a mutable `clan.treasury_balance` field without transaction history.

## 34.2 Core Treasury generation by split

For eligible Clan-context activity, part of the gross reward goes to the player and part goes to Treasury.

Current example mechanism:

```text
Gross activity value = 10
Player = 9
Treasury = 1
```

A 10% split is a **CONFIG example**, not a permanent value.

This split applies only to Clan-context reward events that are configured as Treasury-producing.

## 34.3 Eligible sources

Treasury can be generated by configured Season 1 Clan activities including:

- valid Clan Home activity,
- individual Clan missions,
- group Clan missions,
- other explicitly configured Clan actions.

Normal external Creator/Network Group activity does not automatically fund the member's Clan Treasury unless a future config explicitly adds that behavior.

## 34.4 Atomic settlement

A Clan reward event that splits between player and Treasury must settle atomically so it cannot credit one side and fail the other.

---

# 35. Clan Shop, Inventory and active effects

## 35.1 Shared stackable inventory

Clan Inventory is shared and stackable.

An item has quantity `xN`; purchase is repeatable unless the item definition says otherwise.

## 35.2 Captain purchase permission

Season 1 frontend exposes Shop control to Captain.

Server must enforce purchase/use permission regardless of client state.

## 35.3 Item engine

Generic item definitions should support fields such as:

- kind/category
- price
- rarity
- activation mode
- duration
- stackability
- effect identifier/config payload
- Season availability/feature flag

## 35.4 Timed items

Lifecycle:

```text
Buy
→ Inventory quantity +1
→ Activate
→ quantity -1 atomically
→ ActiveEffect created
→ countdown/effect active
→ expires
```

Activation must be concurrency-safe and idempotent.

## 35.5 Context items

Not every item is a timed booster.

Generic infrastructure should support context-consumed items such as future upgrade modules or event passes without implementing future War behavior.

## 35.6 Defence items in Season 1

Defence items must **not be visible/active Season 1 catalog content**.

However the generic Item/Inventory/Effect architecture must be extensible enough to add Defence items later without redesigning the inventory schema.

Do not seed/activate Season 2 Defence business effects in production Season 1.

---

# 36. Clan achievements / Trophy Hall

Clan achievements are distinct from Player achievements.

Backend should track Clan achievement progress/unlocks for the current Clan/ClanSeason.

Examples already represented in frontend concept include founding/progression/treasury milestones.

Exact targets are `CONFIG`.

Player achievements such as First Clan/First Contribution belong to player Profile achievement state, not duplicated as Clan-owned achievements.

---

# 37. Arena — Season 1

Arena is the canonical competition home.

## 37.1 Season 1 public sections

Season 1 exposes:

- `Warfront` as locked Season 2 preview,
- `Rankings`.

## 37.2 Player Rankings

Must support:

- global/season rank,
- player identity/avatar,
- level,
- season score,
- Clan when applicable,
- own-player standing even outside top list.

## 37.3 Clan Rankings

Must support:

- Clan rank,
- Clan identity/crest,
- Clan level,
- member/capacity summary where required,
- Clan competitive score,
- own Clan standing.

## 37.4 One canonical ranking home

Do not create a second full Clan ranking inside Soldier Discovery or HQ.

Other pages may show rank summary links.

## 37.5 Warfront

Backend Season 1 only needs feature flags/copy/state needed to render it locked.

No War tables, matchmaker, battle workers or attack/defence rules are required.

Do not design speculative War schemas in Season 1 migrations.

---

# 38. Rankings architecture

Rankings are read-heavy and should not calculate a full global sort from PostgreSQL on every request.

The proven Redis leaderboard concepts from the legacy backend are valuable here.

## 38.1 Redis sorted sets / projections

Use Redis for fast Player and Clan season ranking projections.

PostgreSQL remains the durable score Source of Truth/projection base.

## 38.2 Safe rebuild

The legacy implementation's good ideas should be preserved/refactored:

- temporary ZSET rebuild,
- distributed rebuild lock with ownership,
- lock renewal for long rebuilds,
- replay changes that occurred during rebuild,
- safe atomic swap,
- deletion/tombstone handling.

## 38.3 Own-rank lookup

Endpoints must efficiently return the requesting player's/Clan's own rank even when not in the top page.

## 38.4 Reconciliation

Scheduled reconciliation can rebuild Redis from durable PostgreSQL projections so Redis loss does not lose economic truth.

---

# 39. Season model and campaign settlement

## 39.1 Multiple seasons

Dexo Land is expected to run multiple seasons.

Each point event belongs to a season.

## 39.2 Season close

Closing an individual season performs actions such as:

- stop new events for the closing season at the defined boundary,
- finalize/reconcile points,
- freeze rankings,
- snapshot player season standings,
- snapshot Clan season standings,
- award configured season medals/achievements,
- archive Season/ClanSeason state,
- open next-season initialization when appropriate.

## 39.3 No per-season DLX allocation

**DLX allocation does not occur after each season.**

The campaign runs across multiple seasons.

Final DLX allocation happens only after the overall season campaign concludes according to a future final-allocation contract.

Therefore Season 1 close must not mint/allocate final DLX balances.

## 39.4 Lifetime points across seasons

Lifetime Points already include each season's canonical point events.

Do not copy Season Points into Lifetime again during close.

## 39.5 Persistent Clan identity

Clan identity persists; seasonal Clan progression/roster/state is represented through ClanSeason and is archived/reset/reinitialized per season.

---

# 40. Notifications and Telegram DM policy

Dexo Land has both in-app notifications and selected bot direct messages.

Important events that should support Telegram DM include at minimum:

- Clan join request accepted + personal invite link,
- Clan inactivity warning,
- Captain inactivity/recovery warning,
- Clan Home access lost/rejoin prompt,
- important governance/removal/dissolution case updates,
- critical group bot-admin loss for Captain,
- moderation/suspension state where appropriate.

Telegram DM can fail when the user has never opened the bot privately or has blocked it.

Therefore every critical DM-worthy event should also persist an in-app notification/event so state is not lost.

Notification preference/rate policies can be configurable.

---

# 41. Broadcast and admin messaging

The old backend has useful broadcast experience.

Season 1 should support controlled admin messaging to eligible audiences, but it must be refactored for the independent Dexo Land project.

Requirements:

- queued delivery,
- rate-limit awareness,
- retries with limits,
- delivery/failure metrics,
- audience filters,
- safe message logging,
- no exchange-domain dependencies.

---

# 42. Moderation subsystem

Season 1 needs platform-level moderation because Clan governance alone cannot resolve fraud/abuse.

## 42.1 Moderation reports

Players/Admins may create cases with:

- reporter
- target player
- Clan/group context
- reason/category
- message/evidence references
- status
- timestamps

## 42.2 Moderation actions

Supported action concepts:

- dismiss
- warn
- flag
- suspend temporarily
- ban permanently
- remove from Clan
- reward adjustment/reversal using auditable adjustment events when absolutely required

## 42.3 Suspended/Banned players

Suspended/Banned players cannot continue reward-generating gameplay according to policy.

Bans must not erase history.

## 42.4 Audit

Moderator/admin actions must be auditable: actor, reason, before/after state, timestamp.

---

# 43. Draft data model

The final table design belongs to implementation slices, but the following domain entities are expected.

## 43.1 Identity / player

### Player

- Telegram identity
- account/risk status
- first seen/source
- profile metadata

### PlayerProgression

- lifetime points projection
- level/progress projection
- Dexo state

### Season

- identity/status/start/end/config version

### PlayerSeason

- player + season unique
- season points projection
- ranking-related snapshot data

## 43.2 Rewards/config

### RewardLedger

Immutable/auditable reward settlements with idempotency.

### GameConfigVersion

Versioned effective configuration.

## 43.3 Daily/missions

### DailyClaim

Unique per player/reset period.

### MissionDefinition

Admin/configurable mission content/rules.

### PlayerMission

Per-player progress/state/verification.

## 43.4 Typing

### TypingAttempt

- player
- phrase/version
- started/submitted timing as needed
- accuracy
- WPM
- validation status
- calculated pending reward
- idempotency/source identity

### TypingWallet

Claimable current typing amount projection.

### TypingWalletTransaction

Append-only credits from attempts and debits from claims/adjustments.

## 43.5 Referral

### ReferralRelationship

- inviter
- invitee unique relation
- attribution source/time
- pending/verified state

### ReferralProjection/Tier

Derived/configurable tier state.

## 43.6 Telegram/network

### TelegramGroup

Canonical Telegram chat metadata and bot state.

### CreatorGroupRegistration

Creator ownership, status, verification.

### TelegramActivityEvent / Aggregate

Normalized activity/event metadata and aggregation state.

### TelegramMessageLog

Moderation-oriented message evidence with retention policy.

## 43.7 Clan

### Clan

Persistent identity and Telegram-group binding.

### ClanSeason

Season-specific HQ/capacity/score/Treasury state.

### ClanMembership

Season-specific player membership/role/access state and start/end reasons.

### ClanMembershipEvent

Audit trail for lifecycle changes.

### ClanJoinRequest

Soldier application lifecycle.

### ClanInvite

User-specific Telegram invite state.

### ClanContribution

Contribution ledger/projection keyed by member, ClanSeason, source.

### ClanTreasuryLedger

Append-only credits/debits.

### ClanInventory

Item quantities.

### ClanActiveEffect

Timed active effects.

### ClanMission / ClanMissionProgress

Group/individual mission state.

### ClanAchievement

Clan-owned achievement progress/unlock.

### ClanRemovalCase

Captain/governance removal proposal.

### ClanGovernanceVote

Timed removal/dissolution/recovery voting.

### ClanMembershipIncident

Telegram-side unexpected access changes such as direct ban/removal.

## 43.8 Moderation

### ModerationReport

Evidence/case lifecycle.

### ModerationAction

Auditable platform actions.

## 43.9 Notifications

### Notification

Persistent in-app notification with optional Telegram-delivery metadata.

---

# 44. Draft API surface

Exact routes/schemas are implementation work, but the API must cover these responsibilities.

## 44.1 Auth/bootstrap

- Mini App authentication/bootstrap
- current player
- current season/config-safe UI summary

## 44.2 Earn

- daily state/claim
- earn history

## 44.3 Missions

- list missions
- start/open external mission
- verify/sync
- claim

## 44.4 Typing

- get daily typing state
- start attempt/token if required
- submit result
- get attempt history/best
- get claimable balance
- claim typing balance

## 44.5 Referral

- referral summary/link
- invitees
- tier/projection

## 44.6 Creator Groups

- list registered groups
- list eligible/known groups where relevant
- register/verify creator group
- refresh status/admin state
- activity/reward summary

## 44.7 Profile

- overview
- achievements
- collection
- equip/feature frame/title/medal where allowed

## 44.8 Clan

- eligibility/role state
- choose seasonal role
- discovery/search/details
- submit/cancel join request if product allows
- Captain applicant list/review
- eligible Telegram groups
- register Clan
- HQ summary
- missions
- roster
- Treasury
- inventory/shop
- activate/use item
- active effects
- achievements
- generate/regenerate accepted invite
- membership access/rejoin state
- voluntary leave
- removal/governance cases/votes
- recovery state

## 44.9 Arena

- Player ranking pages + own rank
- Clan ranking pages + own Clan rank
- Season summary
- locked Season 2 feature state

## 44.10 Notifications

- list/read/read-all as needed

## 44.11 Telegram webhook

- secured webhook endpoint only

---

# 45. Concurrency and transactional rules

Critical mutations require explicit concurrency tests.

## 45.1 Daily claim

Two simultaneous claims → at most one reward.

## 45.2 Mission claim

Retries/concurrent clicks → at most one reward.

## 45.3 Typing attempt submission

Duplicate submission → one valid attempt/reward accrual.

## 45.4 Typing wallet claim

Concurrent claims → no negative balance, no double credit.

## 45.5 Referral verification/reward

Repeated verification or source reward retries → one relationship transition and one passive reward per eligible source.

## 45.6 Creator activity settlement

Worker retries → no duplicate creator reward window/event settlement.

## 45.7 Clan role/membership

Concurrent role/join actions cannot create multiple active memberships.

## 45.8 Join request acceptance/invite

Repeated accept/retry must not create contradictory active states or unlimited active invite credentials.

## 45.9 Shop purchase

Treasury cannot overspend under concurrency.

## 45.10 Item activation

Quantity and effect creation are atomic.

## 45.11 Treasury split

Player credit + Treasury credit + contribution/accounting must settle consistently in one transaction boundary or recoverable outbox workflow.

## 45.12 Governance vote

A voter gets one valid vote per case. Closing/execution is idempotent.

---

# 46. PostgreSQL, Redis, RabbitMQ and Celery responsibilities

## PostgreSQL

Durable truth for:

- identity,
- memberships,
- config versions,
- reward ledgers,
- Treasury ledger,
- inventory/effects,
- mission/typing/referral state,
- activity evidence required by retention,
- moderation/governance,
- season snapshots.

## Redis

Use for:

- cache,
- rate limiting,
- short-lived idempotency windows where supplementary,
- ephemeral counters,
- ranking projections,
- distributed rebuild coordination,
- short-lived challenge/session state.

Redis loss must be recoverable from PostgreSQL for economic state.

Do not use `KEYS` scans in production workflows.

Use namespaced keys and `SCAN` only where enumeration is genuinely unavoidable.

## RabbitMQ

Celery message broker for durable asynchronous job dispatch/isolation.

## Celery

Use tasks as orchestration wrappers.

Business logic belongs in domain/application services that can be called/tested synchronously.

Do not place giant domain logic blocks directly inside Celery task functions.

---

# 47. Legacy backend reuse policy

## 47.1 Port/refactor these proven ideas

From the old Dalexio Airdrop module, reuse/refactor where appropriate:

- Telegram Mini App HMAC validation approach,
- Telegram webhook secret verification,
- `update_id` dedupe concept,
- referral-code generation concept,
- first-touch referral attribution,
- `my_chat_member` / membership/admin event handling,
- Telegram API wrapper patterns,
- safe log sanitization,
- queued broadcast experience,
- row-lock/idempotent claim patterns,
- Redis leaderboard algorithms,
- safe ranking rebuild lock/temporary-set/replay behavior,
- concurrency/idempotency test ideas.

## 47.2 Do not copy these legacy designs directly

Do not port as the new architecture:

- mutable profile score counters as sole truth,
- Redis `KEYS` scans,
- deleting activity buffers before durable persistence,
- a separate FastAPI webhook app solely because V1 used it,
- hourly claim mechanics,
- old referral activation threshold logic as hard rules,
- old group-add one-time reward as the new Creator Program model,
- old mass-reset season procedure over every profile,
- exchange/account/whitelist coupling,
- old direct `any group message → personal points` behavior,
- old models tightly coupled to exchange/subscription domains.

The old backend is implementation reference material, not something to extend in place.

---

# 48. Security and privacy baseline

1. Verify Telegram Mini App auth server-side.
2. Verify webhook secret.
3. Never trust client-supplied player/Clan ownership claims.
4. Use CSRF/auth strategy appropriate to the Mini App API architecture.
5. Rate-limit public/sensitive mutations.
6. Store secrets only through environment/secret management.
7. Never log Telegram auth hashes, bot tokens or sensitive request headers.
8. Audit moderator/admin economic adjustments.
9. Validate admin permissions server-side.
10. Use least privilege for database/app/container credentials.
11. Apply message-content retention policy and access control.
12. Keep raw Telegram payload retention minimal unless needed for debugging/evidence.

---

# 49. Observability

Production readiness requires structured observability.

At minimum expose/collect:

- request latency/error rates,
- webhook received/deduped/rejected counts,
- queue depth/age,
- worker failure/retry rates,
- Telegram API error/rate-limit metrics,
- reward settlement counts/failures,
- fraud/rejected/suspicious activity counts,
- creator group active/admin-required counts,
- typing attempt/claim metrics,
- Clan membership/recovery/governance metrics,
- leaderboard rebuild duration/failures,
- PostgreSQL slow queries/connection pressure,
- Redis health/memory,
- RabbitMQ health.

Use structured logs with correlation/event IDs where practical.

---

# 50. Admin and Game Configuration

Django Admin or a controlled admin surface should allow authorized operators to manage configuration/content without code deploys where appropriate.

Examples:

- Season definitions/state
- Mission definitions
- reward amounts/formulas/config values
- referral tiers/eligible sources
- Network activity rate/fraud config
- Clan activity economy/fraud config
- Clan unlock/capacity/inactivity/governance config
- Treasury split
- item catalog/availability/pricing/durations
- achievements/medals thresholds
- notification/broadcast content
- player moderation state
- manual audited adjustment actions

Configuration changes affecting reward economics should be versioned/effective-dated so historical reward calculations remain explainable.

---

# 51. Testing requirements

Tests are not optional scaffolding; they are part of the product contract.

## 51.1 Unit tests

Domain rules and state transitions.

## 51.2 Integration tests

Django ORM, transactional behavior, API permissions/authentication, Celery service boundaries where appropriate.

## 51.3 Telegram fixture tests

Representative Telegram update payloads for:

- `/start`
- message
- group add/admin changes
- user membership changes
- duplicate updates

## 51.4 Concurrency/idempotency tests

Required for every critical mutation listed in Section 45.

## 51.5 Migration checks

CI must ensure migrations are committed and consistent.

## 51.6 Ranking tests

Test:

- stable ordering/tie rules,
- own-rank lookup,
- rebuild under concurrent updates,
- Redis recovery/rebuild.

## 51.7 Anti-fraud tests

Rule classification and configurable thresholds must have deterministic tests.

---

# 52. GitHub Actions / PR quality gate

Every development phase is a hard-gated delivery unit. GitHub Actions is mandatory for every phase, and every phase must have its own dedicated pull request with CI coverage appropriate to that phase's scope.

Required progression:

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

A pull request with any failing, cancelled, missing or required-skipped check is incomplete. CI failures must be corrected in the same phase and pull request. Passing CI is necessary but does not authorize progression by itself. Once all required checks are green, the coding agent must stop; it must not merge the pull request or start the next phase unless the repository owner explicitly authorizes that action. The owner provides the green pull request URL to an independent reviewer before approving merge or progression.

As later phases introduce the application stack, their baseline checks should include, as appropriate:

- formatting/lint
- static/type checks as chosen for Python project
- Django system checks
- migration drift/check
- unit tests
- integration tests
- concurrency/idempotency tests when touching critical mutations
- Docker image build
- OpenAPI/schema generation/validation when API changes
- dependency/security checks where practical

No PR should be merged solely because happy-path tests pass when it changes rewards, membership, Treasury or governance.

---

# 53. Season 1 development roadmap

War/Defence is deliberately absent.

## Phase 0 — Contract and Delivery Gate

Scope:

- Source of Truth
- root `AGENTS.md`
- branch conventions
- Phase 0 GitHub Actions validation
- mandatory phase CI + owner approval rule

Phase 0 ends only after:

1. its pull request exists;
2. all required CI checks are green;
3. the repository owner approves it.

## Phase 1 — Foundation

- new independent repository/codebase
- Docker/Compose
- Django + Django Ninja
- PostgreSQL
- Redis
- RabbitMQ
- Celery worker/beat
- env/settings structure
- health/readiness endpoints
- structured logging
- pytest
- lint/type tooling
- GitHub Actions
- base admin

No game economy implementation in Phase 1.

## Phase 2 — Telegram Identity & Ingestion

- Player identity
- three first-seen/onboarding paths
- Mini App auth
- `/start`
- referral attribution plumbing
- webhook security/dedupe
- normalized Telegram events
- TelegramGroup model
- bot/user membership/admin tracking
- message log retention foundation
- queue processing

## Phase 3 — Season, Progression, Config & Rewards

- Season / PlayerSeason
- canonical points/lifetime projections
- level progression
- Dexo progression projection
- GameConfig versions
- Reward Ledger
- activity/history projection
- bootstrap API

## Phase 4 — Daily, Missions & Typing

- daily claim/streak
- Mission definitions/state
- INTERNAL/TELEGRAM/TIMED_EXTERNAL verification
- Typing attempts
- variable reward formula interface
- Typing claimable wallet
- atomic claim

## Phase 5 — Network

### Referral

- referral code/link
- first-touch relationship
- pending/verified lifecycle
- tier config/projection
- passive reward infrastructure

### Creator Group Program

- creator registrations
- bot Admin qualification
- activity engine
- anti-fraud classifications
- Network economy policy
- creator reward settlement
- speaker reward capability with initial zero/disabled config

## Phase 6 — Profile & Collection

- achievements
- Season 1 Clan-related player achievements
- medals
- frames/titles
- customization
- profile summary

## Phase 7 — Clan Core

- configurable Clan gate
- Captain/Soldier role
- persistent Clan + ClanSeason
- eligible Telegram groups
- Clan registration
- discovery/search
- join requests
- applicant review
- user-specific invite/DM
- Telegram join verification
- membership/access lifecycle
- voluntary leave/cooldown
- inactivity tracking
- removal/governance cases
- moderation integration
- Recovery Mode/dissolution vote

## Phase 8 — Clan Progression & Economy

- HQ state/progression
- capacity
- Clan missions
- contribution
- Treasury split + ledger
- Shop
- stackable Inventory
- generic item/effect engine
- active effects
- Clan achievements

No War/Defence effects.

## Phase 9 — Arena Rankings

- Player season ranking
- Clan season ranking
- Redis projections
- safe rebuild/reconciliation
- own-rank lookup
- locked S2 Warfront flag/state only

## Phase 10 — Notifications, Broadcast & Admin

- in-app notifications
- selected Telegram DMs
- admin config/content polish
- broadcast
- moderation operations

## Phase 11 — Season Close & Production Hardening

- ranking freeze/snapshots
- medals/season honors
- no DLX allocation yet
- load tests
- DB/index tuning
- worker/queue tuning
- backup/restore drills
- Redis rebuild drills
- failure/retry tests
- security review
- deployment/release checklist

---

# 54. Economy & Balance values intentionally left configurable

The following are mechanisms already approved, but values/formulas may be finalized later without blocking engineering:

- level thresholds/curve
- Dexo evolution thresholds
- Clan unlock threshold (frontend sample: Level 5)
- Daily reward amount/reset boundary
- Mission reward amounts
- Timed-external minimum dwell (example: ~6 seconds)
- Referral activation threshold
- Referral tier thresholds
- Referral percentages
- referral-share-eligible event types
- Creator reward rates
- Network Speaker reward rates (initially disabled/zero)
- Network max messages/minute (example: 5)
- Network max rewardable characters/message (example: 200)
- duplicate/abuse thresholds
- Clan activity reward rates
- Clan anti-fraud thresholds
- Typing attempts/day (current default concept: 5)
- Typing run duration
- accuracy/WPM reward formula
- typing anti-cheat thresholds
- Clan initial capacity
- Clan capacity curve/max
- Clan HQ XP curve
- Clan Contribution mapping
- Treasury split percentage (example mechanism: 10%)
- Captain leadership bonus
- item prices/durations
- Soldier inactivity warning/removal thresholds (example concept: ~3/~5 days)
- Captain inactivity warning/recovery thresholds
- voluntary leave cooldown/max leaves
- governance vote duration/quorum/approval thresholds
- message retention window
- ranking page size/tie-break rules
- notification throttling

Agents must implement these as config, not block development or hardcode them into domain services.

---

# 55. Decisions that are no longer open

For clarity, agents must not reopen these unless the product owner explicitly changes them:

- Final frontend is the Season 1 product-scope Source of Truth.
- Dexo Land is an independent project.
- One Telegram bot.
- No whitelist.
- No exchange dependency.
- No active wallet integration in Season 1.
- No Officer role.
- Navigation: Base / Earn / Clan / Arena / Profile.
- Earn: Earn / Network / Typing.
- Network: Referral + Creator Group Program.
- Bot Admin is mandatory for reward-producing Creator Groups.
- Users can be created from `/start`, Mini App auth or a group message.
- Speaker reward capability exists but starts disabled/zero in normal Network groups.
- No general daily cap for conversation activity; rate/content/anti-fraud controls are used.
- Network activity and Clan activity share an engine but have different configs.
- Mission verification families: INTERNAL / TELEGRAM / TIMED_EXTERNAL.
- Typing reward varies by accuracy/speed.
- Typing rewards accumulate in a claimable balance and can be claimed after any number of attempts.
- Clan Season 1 roles: Captain / Soldier only.
- Clan Ready, First Clan and First Contribution are Season 1 achievements.
- First War / First Victory are future.
- Telegram Group membership is not the same as Dexo Land Clan membership.
- Accepted Clan request leads to a bot DM with a user-specific invite and Telegram membership verification.
- Expired invite links can be regenerated; invite expiry does not delete membership.
- Voluntary Clan leave is allowed but controlled by cooldown/friction.
- Direct Clan switching is not allowed.
- Contribution remains historically attached to the Clan where it was earned.
- Captain cannot unilaterally bypass governance by banning users in Telegram.
- Community removals use timed governance; serious violations use platform moderation.
- Soldier inactivity can warn then remove according to config.
- Captain inactivity enters Recovery Mode and can trigger a dissolution/release vote.
- No Captain succession in Season 1.
- Captain is included in Clan Contribution.
- Captain receives a separate configurable leadership bonus.
- Treasury is generated from configured Clan activities through a configurable split of gross activity value.
- Contribution is measured from gross eligible Clan value before Treasury split unless later explicitly changed.
- Defence items are not shown/active in Season 1; only generic extensible inventory/effect infrastructure exists.
- Player and Clan rankings are canonical in Arena.
- Warfront remains locked/preview only in Season 1.
- No War/Defence backend development in Season 1.
- Season Points and Lifetime Points are projections of the same events.
- Final DLX allocation occurs only after the overall multi-season campaign, not after each season.
- PostgreSQL is durable truth.
- Redis is not economic truth.
- RabbitMQ is the Celery broker.
- Webhook is ingress, not scoring.
- Reward/accounting mutations are idempotent and auditable.

---

# 56. Definition of Done for backend implementation

Season 1 backend is complete only when:

1. Final Mini App flows can be backed by real server-owned state.
2. No production route depends on Zustand/mock values for authoritative game outcomes.
3. Every reward path is idempotent/auditable.
4. Telegram identity and group/admin state are server-verified.
5. Activity fraud rules are configurable and observable.
6. Typing claim wallet is atomic and concurrency-tested.
7. Clan membership survives/reconciles Telegram access edge cases correctly.
8. Clan governance/moderation edge cases are tested.
9. Treasury cannot double-credit/overspend.
10. Player/Clan rankings recover from Redis loss.
11. Season close does not double-count Lifetime Points and does not allocate DLX.
12. War/Defence code has not leaked into Season 1 backend scope.
13. Docker/CI/test/backup/observability requirements pass.
14. Production configuration values can be tuned without editing domain business logic.

---

# 57. Instruction to coding agents

Before making any code change, read the repository root `AGENTS.md` and this file.

If another document or old code conflicts with this Source of Truth:

1. do not silently reconcile it,
2. follow this document and the final frontend scope,
3. surface the conflict in the PR description,
4. do not invent a feature or numeric rule.

Implement one scoped slice at a time and keep the architecture extensible for later seasons without prematurely building Season 2 systems.

**Season 1 development may begin from this contract.**

---

# Appendix A — Final frontend contract map (implementation aid)

This appendix records the final reviewed frontend structure so backend agents do not depend on stale Phase reports.

It is **not** permission to hardcode fixture numbers.

## A.1 Main navigation

```text
Base
Earn
Clan
Arena
Profile
```

## A.2 Earn navigation

```text
Earn
Network
Typing
```

### Earn/Daily/Mission fixture concepts currently represented

Current frontend mission identities include:

- Explore Dalexio (`visit-dalexio`)
- View the latest X update (`view-x-update`)
- Open/join Telegram community (`join-telegram`)
- Complete a Typing Arena run (`typing-session`)
- Review explorer profile (`complete-profile`)
- Read Season One brief (`read-season-brief`)

Their current frontend reward values are fixture/sample economy and must be supplied by mission configuration in backend.

The Telegram mission must use the approved `TELEGRAM` verification strategy rather than treating its current frontend `external` mock action as final backend verification.

External website/X missions use `TIMED_EXTERNAL` unless later explicitly assigned another verification policy.

## A.3 Network

Two product sections:

```text
Referral
Creator / Telegram Groups
```

Current frontend displays counts/history and sample rewards. Backend uses the contracts in Sections 16–19 rather than copying frontend mock mutations.

## A.4 Typing

Final frontend already exposes:

- target phrase,
- timer,
- WPM,
- accuracy,
- current day's attempts,
- best result,
- daily attempt limit presentation.

Backend overrides the old mock fixed-reward behavior with the approved variable reward + claimable wallet contract.

## A.5 Profile

```text
Overview
Achievements
Collection
```

Current title concepts:

- Explorer
- Early Pioneer
- Mission Runner
- Network Builder
- Community Voice
- Season Veteran

Current seasonal medal concepts:

- Founding Season
- Season Participant
- Season Veteran
- Top 10 Percent
- Top 1 Percent
- Season Champion

Current career medal concepts:

- Activity Veteran
- Clan Loyalty
- Leadership
- Mission Master

Current frame concepts:

- Standard
- Premium
- Seasonal
- Founder
- Clan
- Captain

Unlock/availability behavior must follow the approved Season 1 contract and configuration, not only the old `availableNow` mock flag. In particular, Clan Ready / First Clan / First Contribution are approved Season 1 player achievements even though an older frontend catalog snapshot marked them unavailable.

The earlier First Verified Activity fixture (`200` verified messages, multi-day progress, previous sample counting caps) is achievement fixture/economy data. The current approved product rule is that there is **no generic daily conversation reward cap**; anti-fraud/rate/content rules determine valid activity. Achievement-specific counting rules may still be configured independently.

## A.6 Clan user-visible states

### Pre-unlock

- locked Clan screen until configured progression gate (frontend currently presents Level 5)

### Role selection

- Captain
- Soldier

No Officer.

### Soldier before membership

```text
Discover
Search
→ Clan detail/preview
→ request join
→ pending/rejected state
→ accepted invite flow
```

Full ranking is intentionally absent from Soldier Discovery.

### Captain before Clan creation

```text
Captain selected
→ eligible Telegram Groups
→ verify group/admin/bot state
→ Clan identity form
→ register Clan
```

### Clan HQ tabs

```text
HQ / Home
Missions / Tasks
Roster / Members
Inventory
```

Captain additionally controls Shop/applicant review/actions represented in frontend.

## A.7 Clan item fixture catalog

The final frontend contains object definitions for:

### Boosters

- Clan XP Booster
- Contribution Booster
- Mission Progress Booster
- Coordination Booster

### Defence objects

- Early Warning Beacon
- Emergency Repair Kit
- Firewall Shield
- Protection Dome

**Approved override:** Defence items are not visible/active Season 1 product catalog. Their backend business effects are not implemented. Generic item infrastructure remains extensible.

### Utility objects

- Alliance Permit
- Capacity Expansion Module
- Roster Slot Module
- Event Entry Pass
- HQ Upgrade Kit

Some utility descriptions anticipate later systems. Season 1 may expose only the utility actions that have a real Season 1 context. A future-context utility must not cause an Alliance/World Event backend to be invented.

Prices, durations, rarity and effect amounts in the frontend catalog are fixture/config data.

## A.8 Clan achievement concepts

Frontend Clan-owned Trophy Hall fixture concepts include:

- Banner Raised
- United Front
- Treasury Keeper

Targets are configurable.

Player-owned Clan achievements are managed separately in Profile.

## A.9 Arena Season 1

Public Season 1 structure:

```text
Warfront — locked / S2
Rankings
   ├─ Players
   └─ Clans
```

Frontend contains a fully built Season 2 diagnostic War experience. That diagnostic code is explicitly **not** a Season 1 backend contract.

## A.10 Diagnostics

Diagnostics and scenario stores are QA/development fixtures.

An agent must not infer a production backend feature merely because Diagnostics can render it.

---

# Appendix B — Suggested configuration shape (non-binding names)

The exact storage/schema can evolve, but agents should aim for a coherent versioned config instead of scattered settings.

Example conceptual shape:

```yaml
season:
  id: founding-season

progression:
  level_curve: ...
  clan_unlock_level: 5  # sample/default, configurable

daily:
  reward_points: ...
  reset_timezone: ...

missions:
  timed_external_min_seconds: 6  # sample/default

typing:
  attempts_per_day: 5            # current product default concept
  run_seconds: ...
  reward_formula: ...
  min_accuracy: ...
  min_wpm: ...

referral:
  activation_rule: ...
  tiers: ...
  eligible_reward_sources: ...

network_activity:
  speaker_reward:
    enabled: false
    ...
  creator_reward: ...
  max_messages_per_minute: 5     # sample
  max_rewardable_chars: 200      # sample

anti_fraud:
  network: ...
  clan: ...

clan:
  initial_capacity: ...
  capacity_curve: ...
  inactivity:
    soldier_warning_after: ...
    soldier_remove_after: ...
    captain_warning_after: ...
    captain_recovery_after: ...
  leave:
    cooldown: ...
    max_voluntary_leaves_per_season: ...
  governance:
    vote_duration: ...
    quorum: ...
    approval_threshold: ...
  treasury:
    split_rate: 0.10             # example mechanism only
  captain_bonus: ...

moderation:
  message_retention: ...
```

No agent should treat the illustrative values in this appendix as final production economics.
