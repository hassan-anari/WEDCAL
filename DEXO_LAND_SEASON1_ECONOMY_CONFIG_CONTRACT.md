# Dexo Land — Season 1 Economy & Balance Contract

**Status:** APPROVED BASELINE FOR IMPLEMENTATION  
**Contract version:** `1.0.0`  
**Machine-readable companion:** `config/examples/season1.game-config.v1.json`  
**Applies to:** Dexo Land Season 1 backend and future Season 1 GameConfig versions  

---

## 0. Authority and purpose

This document finalizes the initial Season 1 economy/balance values that were intentionally left configurable in `DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md`.

It does **not** replace the Product & Backend Source of Truth. The authority order is:

1. `DEXO_LAND_SEASON1_SOURCE_OF_TRUTH_EN.md` — product/domain behavior and scope.
2. This Economy & Balance Contract — numeric balance, formulas, thresholds and tunable policy for Season 1.
3. The machine-readable JSON — canonical initial values for `GameConfigVersion`.

If this document conflicts with the Source of Truth on a product invariant, the Source of Truth wins. If JSON interpretation is ambiguous, this document defines the intended semantics.

Frontend fixture numbers remain non-authoritative unless this contract explicitly adopts them. Some frontend-facing identifiers are preserved intentionally to prevent needless translation layers.

---

## 1. Change-control rules

1. All player-point amounts are integers.
2. Percentages are stored as integer **basis points** (`10000 = 100%`).
3. No reward/economy calculation may use binary floating-point as economic truth.
4. Once a `GameConfigVersion` is published, it is immutable.
5. New balance values apply only through a new effective-dated config version.
6. Historical RewardLedger entries are never recalculated under a newer config.
7. PostgreSQL/RewardLedger remain canonical; Redis is a disposable projection/cache.
8. A reward-producing source must have deterministic idempotency.
9. Agents must not embed values from this contract as scattered domain constants; runtime values come from the resolved config snapshot.
10. Future phases may add schema validation for their section, but must preserve the full snapshot and existing semantics.

---

# 2. Balance goals

Season 1 is **45 days**: six seven-day economic epochs (42 distribution days) followed by a three-day closing/reconciliation window. There is no seventh weekly DLX epoch. Existing point amounts and Level thresholds are preserved in this contract-sync revision; the old 90-day balancing assumption is obsolete.

Expected end-of-season ranges:

| Player behavior | Expected Season Points | Approx. level |
|---|---:|---:|
| Casual | 12k–18k | 10–12 |
| Regular/Core | 30k–40k | 15–17 |
| Highly active | 45k–65k | 18–20 |
| Strong social/referral | 65k–100k | 21–24 |
| Strong creator | 90k–150k+ | 23–27 |
| Exceptional top player | 200k+ | 30+ target boundary |

Primary progression targets:

- Median active user reaches Level 5 around day 5–8.
- Core user reaches Level 10 around week 4.
- Core-player Level targets remain provisional telemetry targets; this revision does not rebalance the Level curve.
- Level 25+ should be uncommon during Season 1.
- Level 30 should be exceptional, not a routine completion target.

Lifetime Points persist across seasons, so Season 1 is not intended to exhaust permanent progression.

---

# 3. Canonical point semantics

There is one canonical player-point reward event.

```text
Reward Event +X
    ├─ current PlayerSeason.season_points +X
    └─ PlayerProgression.lifetime_points +X
```

Season close must not add Season Points into Lifetime Points again.

`POINT`, `XP`, `Season Points`, and frontend reward-XP labels are views of the same canonical player-point event unless a separate non-player accounting unit is explicitly defined below (Treasury, Contribution, HQ XP, Typing Claimable Balance).

---

# 4. Player level curve

| Level | Min Lifetime Points |
|---:|---:|
| 1 | 0 |
| 2 | 300 |
| 3 | 800 |
| 4 | 1,500 |
| 5 | 2,500 |
| 6 | 3,800 |
| 7 | 5,400 |
| 8 | 7,300 |
| 9 | 9,500 |
| 10 | 12,000 |
| 11 | 14,800 |
| 12 | 18,000 |
| 13 | 21,600 |
| 14 | 25,600 |
| 15 | 30,000 |
| 16 | 34,900 |
| 17 | 40,300 |
| 18 | 46,200 |
| 19 | 52,700 |
| 20 | 59,800 |
| 21 | 67,600 |
| 22 | 76,200 |
| 23 | 85,700 |
| 24 | 96,200 |
| 25 | 108,000 |
| 26 | 121,500 |
| 27 | 137,000 |
| 28 | 155,000 |
| 29 | 176,000 |
| 30 | 200,000 |


The curve intentionally has three pacing zones:

- Levels 1–5: fast onboarding and access unlocks.
- Levels 6–15: steady core progression.
- Levels 16–30: long-term/social/competitive progression.

**Clan unlock:** Level 5 (`2,500` Lifetime Points).  
**Referral verification progression gate:** Level 5.

---

# 5. Dexo evolution

The final frontend defines these exact Dexo stage keys. Do not invent an extra stage that the final frontend cannot render.

| Dexo stage | Minimum Level |
|---|---:|
| `newborn` | 1 |
| `explorer` | 5 |
| `connector` | 10 |
| `specialist` | 15 |
| `vanguard` | 20 |
| `legend` | 30 |

Dexo stage is derived state, not a spendable currency.

---

# 6. Daily reward

Reset boundary: **00:00 UTC**.

Seven-day reward cycle:

| Cycle day | Points |
|---:|---:|
| 1 | 80 |
| 2 | 90 |
| 3 | 100 |
| 4 | 110 |
| 5 | 120 |
| 6 | 140 |
| 7 | 180 |

Weekly total: **820 Points** (~117/day average).

Rules:

- Day 7 completion restarts the reward cycle at Day 1.
- The actual consecutive-day streak counter continues beyond Day 7.
- Missing a complete claim period resets the streak and next reward cycle to Day 1.
- No catch-up/backfill claim in Season 1.
- Concurrent duplicate claims settle at most once.

This preserves 7-day and 30-day streak achievements without turning the Daily reward into the dominant point source.

---

# 7. Mission economy

Season 1 verification families remain exactly:

- `INTERNAL`
- `TELEGRAM`
- `TIMED_EXTERNAL`

Default TIMED_EXTERNAL minimum server-observed dwell: **6 seconds**.

Reward bands:

| Band | Points |
|---|---:|
| MICRO | 40 |
| SMALL | 80 |
| MEDIUM | 120 |
| LARGE | 200 |
| PREMIUM | 300 |

Any proposed ordinary mission above **500 Points** requires an explicit economy review/new config version.

Initial mission values:

| Mission ID | Verification | Points | Repeat |
|---|---|---:|---|
| `visit-dalexio` | TIMED_EXTERNAL | 120 | per definition version |
| `view-x-update` | TIMED_EXTERNAL | 80 | per definition version |
| `join-telegram` | TELEGRAM | 200 | per definition version |
| `typing-session` | INTERNAL | 40 | daily |
| `complete-profile` | INTERNAL | 200 | per definition version |
| `read-season-brief` | INTERNAL | 120 | per definition version |

Mission verification and mission reward settlement remain separate/idempotent operations.

---

# 8. Typing Arena

The final frontend visibly uses a **30-second** challenge, five daily attempts, and an 85% / 10 WPM pass floor. The backend economy is aligned to that visible UX.

## 8.1 Eligibility

- Daily completed-attempt limit: **5**.
- Reset: **00:00 UTC**.
- Challenge window: **30 seconds**.
- Full phrase completion required.
- Minimum Accuracy: **85%** (`8500 bps`).
- Minimum WPM: **10**.
- An invalid completed run consumes the attempt.
- A server/technical failure does not consume the attempt.

## 8.2 Reward formula

```text
Typing reward = Accuracy component + WPM component
```

Accuracy component:

| Accuracy | Points |
|---|---:|
| 85.00–89.99% | 10 |
| 90.00–94.99% | 20 |
| 95.00–97.99% | 30 |
| 98.00–99.99% | 40 |
| 100% | 50 |

WPM component:

| WPM | Points |
|---|---:|
| 10–29 | 5 |
| 30–44 | 10 |
| 45–59 | 20 |
| 60–74 | 30 |
| 75–99 | 35 |
| 100+ | 40 |

Maximum valid reward: **90 Points / attempt**, theoretical max **450/day**.

A typical strong-but-normal run around 95% / 45 WPM earns ~50 Points.

## 8.3 Claimable wallet

Attempt rewards credit Typing Claimable Balance first. Claiming moves the balance into canonical RewardLedger points atomically.

- Claim is allowed after any number of attempts.
- Daily attempt reset does not erase claimable balance.
- Claimable credit retains its source Season.
- At Season close, remaining valid claimable credit is automatically settled to its **source Season**, preventing Season 1 Typing work from becoming Season 2 ranking points.

## 8.4 Anti-cheat thresholds

- WPM >= 160: `SUSPICIOUS`.
- WPM >= 220: `REJECTED`.
- Impossible timing: rejected.
- Replayed attempt identity: rejected.
- `SUSPICIOUS` never means automatic ban by itself.

---

# 9. Referral economy

## 9.1 Activation

Referral attribution remains immutable first-touch.

An invitee becomes verified when all are true:

- Level >= 5.
- Account age >= 24 hours.
- Anti-Sybil checks pass.
- Player is not suspended/banned.

Activation bonus:

- Inviter: **+100 Points**.
- Invitee: **+50 Points**.

These activation rewards are not recursively referral-share eligible.

## 9.2 Tiers

| Tier | Verified referrals | Passive share |
|---|---:|---:|
| Bronze | 1–4 | 3% (300 bps) |
| Silver | 5–14 | 5% (500 bps) |
| Gold | 15+ | 7% (700 bps) |

Passive share is additional mint; it is **not deducted** from the invitee.

Share base: the invitee's final `player_amount`, not gross Clan value or Treasury share.

Eligible source families:

- Daily
- Mission
- Typing Claim
- Clan Activity player amount
- Clan Mission player amount

Explicitly ineligible:

- Referral rewards
- Creator rewards
- Captain leadership bonus
- Achievement/Medal grants
- Admin adjustments
- Treasury transactions

A referral reward can never generate another referral reward.

---

# 10. Network Creator / Group economy

Network and Clan activity are different contexts and use different economic config.

## 10.1 Qualified Network activity

Initial qualification values:

- Minimum message length: **20 characters**.
- Maximum rewardable characters/message: **200**.
- Maximum candidate messages/minute/player: **5**.

This is a rate/quality filter, not a generic daily conversation cap.

## 10.2 Speaker reward

Initial Season 1 config:

```text
speaker_reward.enabled = false
speaker_reward = 0
```

The architecture must still support future activation by config version.

## 10.3 Creator reward

Creator's own activity is excluded from creator reward.

Per registered group:

- Settlement window: **15 minutes**.
- **600 qualified characters = 1 Creator Point**.
- At least **2 unique valid speakers** in the window.
- Maximum **20 Creator Points / window / group**.
- Maximum **3 active creator groups / creator**.
- Safety ceiling: **1,500 Creator Points / creator / day** across groups.

The safety ceiling caps reward issuance, not conversation/activity ingestion.

If bot Admin qualification is lost, reward settlement pauses immediately according to the existing product contract.

---

# 11. Activity anti-fraud defaults

Network and Clan retain separate configuration even where initial values match.

Initial duplicate-content controls:

- Exact duplicate observation window: 10 minutes.
- Near-duplicate observation window: 5 minutes.
- Same fingerprint occurrence 2 in window: suspicious signal.
- Same fingerprint occurrence 4 in window: reward rejection threshold.
- Suspicious classification does not auto-ban.

These are reward-validity controls, not general moderation verdicts.

---

# 12. Clan unlock and capacity

Clan access unlocks at **Level 5** for both Captain/Soldier flows.

Base HQ curve:

| HQ Level | Cumulative HQ XP | Base capacity |
|---:|---:|---:|
| 1 | 0 | 20 |
| 2 | 1,000 | 25 |
| 3 | 3,000 | 30 |
| 4 | 7,000 | 40 |
| 5 | 15,000 | 50 |

Item-enhanced hard capacity maximum: **65** for Season 1.

No fixed global 100-member rule is revived.

---

# 13. Clan Home activity economy

`CLAN_HOME` initial qualification:

- Minimum message: 20 characters.
- Maximum rewardable characters/message: 200.
- Maximum candidate messages/minute/player: 5.
- Settlement aggregation window: 15 minutes.
- **150 qualified characters = 1 gross Clan Point**.
- Maximum **15 gross points/player/window**.
- No generic daily conversation cap.

A gross Clan reward is the economic source event from which player amount, Treasury, Contribution and HQ/Clan projections are derived.

---

# 14. Clan Treasury and Contribution

## 14.1 Treasury split

Treasury share: **10% (1000 bps)**.

Split occurs on aggregated settlement, with Treasury rounded down:

```text
Treasury = floor(gross * 10%)
Player   = gross - Treasury
```

Example:

```text
Gross = 100
Player = 90
Treasury = 10
Contribution = 100
```

Treasury spending never directly mints player Points.

## 14.2 Contribution

- `1 gross Clan Point = 1 Contribution Unit`.
- Contribution is measured **before Treasury split**.
- `5 Contribution Units = 1 HQ XP`.
- `1 Contribution Unit = 1 Clan competitive score unit` initially.

Contribution is historical participation, not a spendable balance.

---

# 15. Captain leadership bonus

Captain leadership reward:

- **2% (200 bps)** of eligible gross Clan activity produced by **other** active members.
- Maximum **500 Points/day**.
- Captain's own activity is excluded from the leadership base.
- Leadership bonus does not create Contribution.
- Leadership bonus is not referral-share eligible.
- It is a separate audited RewardLedger source.

This creates meaningful leadership value without making Captain a passive jackpot.

---

# 16. Clan mission reward bands

| Band | Participant gross Points | Direct HQ XP |
|---|---:|---:|
| SMALL | 80 | 15 |
| MEDIUM | 150 | 30 |
| LARGE | 250 | 60 |
| GROUP | 100 per eligible participant | 200 shared direct HQ XP |

Participant gross reward still goes through the normal Treasury split and Contribution mapping unless a specific mission definition explicitly says otherwise in a later config version.

---

# 17. Clan Shop / Inventory economy

Frontend item IDs/names are preserved. Frontend fixture prices have been adopted as the initial S1 prices where an item is used.

Timed boosters do **not** directly multiply player point rewards; they affect Clan progression/contribution/mission systems to avoid a self-funding point-mint loop.

| Item | Price | S1 | Effect |
|---|---:|:---:|---|
| `rally-beacon` — Clan XP Booster | 2,400 | yes | +20% HQ XP, 120m |
| `contribution-booster` | 2,800 | yes | +15% Contribution, 120m |
| `mission-progress-booster` | 3,100 | yes | +20% Clan mission progress, 90m |
| `coordination-booster` | 3,300 | yes | +10% HQ XP +10% Clan mission progress, 90m |
| `capacity-expansion-module` | 4,100 | yes | +5 Season capacity, max 2/Season |
| `roster-slot-module` | 3,800 | yes | +1 Season capacity, max 5/Season |
| `command-relay` — HQ Upgrade Kit | 5,200 | yes | +2,000 HQ XP, max 2/Season |
| Early Warning / Repair / Firewall / Protection | fixture | no | Defence future |
| Alliance Permit | 3,400 | no | Alliance future |
| Event Entry Pass | 2,200 | no | World Event future |

Timed effects use **highest bonus per effect category**, not multiplicative stacking of the same effect.

Purchase and activation/use are separate atomic transactions. Purchase adds one inventory quantity. Use consumes one quantity and creates the appropriate ActiveEffect/context result.

---

# 18. Clan lifecycle/governance defaults

These are not player-point rewards, but they are balance parameters that materially affect farming and Clan stability.

- Soldier inactivity warning: **3 days**.
- Soldier removal eligibility: **5 days**.
- Captain inactivity warning: **3 days**.
- Captain Recovery state: **7 days**.
- Voluntary leave cooldown: **48 hours**.
- Maximum voluntary leaves/Season: **3**.
- Governance vote duration: **24 hours**.
- Quorum: **40% (4000 bps)**.
- Approval threshold: **60% (6000 bps)**.
- Minimum eligible voters: **3**.
- Telegram access rejoin grace: **48 hours**.
- Bot Admin restore grace before deeper recovery escalation: **72 hours**.

These values do not authorize Captain succession; Season 1 still has no succession system.

---

# 19. Player achievements, collection and point inflation

Achievements/medals are identity, collection and status systems. They do **not** grant player Points in canonical Economy v1.

Season 1 achievements:

- First Mission — target 1.
- First Referral — target 1 verified referral.
- Seven Day Streak — target 7.
- Thirty Day Streak — target 30.
- First Verified Activity — first valid verified activity (`target = 1`), replacing stale frontend fixture wording that referenced 200 messages.
- Clan Ready — Level 5.
- First Clan — first active Clan membership.
- First Contribution — first valid Contribution Unit.
- First War / First Victory — unavailable in S1.

Point reward for all: **0**.

## 19.1 Titles

- Explorer — default.
- Early Pioneer — Founding Season medal.
- Mission Runner — First Mission achievement.
- Network Builder — First Referral achievement.
- Community Voice — First Verified Activity achievement.
- Season Veteran — Season Veteran medal.

## 19.2 Frames

- Standard — default.
- Premium — Level 20.
- Seasonal — Season Participant medal.
- Founder — Founding Season medal.
- Clan — active Clan membership.
- Captain — active Captain role.

---

# 20. Season and career medals

Medals do not mint Points after ranking freeze.

Initial rules:

- `season-founding`: participated in the Founding Season.
- `season-participant`: >= **5,000 Season Points**.
- `season-veteran`: >= **30,000 Season Points**.
- `season-top-10`: final top **10%**.
- `season-top-1`: final top **1%**.
- `season-champion`: final rank **#1**.
- `activity-veteran`: >= **1,000 verified activity events**.
- `clan-loyalty`: >= **30 active days in the same Clan**.
- `leadership`: Captain of a Clan that reaches HQ Level 5.
- `mission-master`: >= **50 claimed missions**.

Season-close Phase 11 evaluates/finalizes season medals after ranking freeze.

---

# 21. Clan Trophy Hall baseline

Clan-owned achievements do not mint player Points:

- `banner-raised`: Clan established and first shared milestone completed.
- `united-front`: >= **25,000 total Season Contribution**.
- `treasury-keeper`: >= **20,000 Treasury earned during the Season** (earned total, not current unspent balance).

Using earned total prevents spending Treasury from erasing achievement progress.

---

# 22. Rankings

- Default page size: 50.
- Maximum page size: 100.
- Minimum score to enter a ranking projection: 1.

Player tie-break order:

1. Season Points descending.
2. Timestamp when the tied score was reached, earlier first.
3. Player ID ascending.

Clan tie-break order:

1. Clan Score descending.
2. Timestamp when the tied score was reached, earlier first.
3. Clan ID ascending.

Ranking is a projection; PostgreSQL/RewardLedger/Clan accounting remain canonical.

---

# 23. Retention defaults relevant to activity economy

- Full monitored message content: **14 days**.
- Fingerprint/risk metadata: **90 days**.
- Evidence attached to a moderation case: retain until case close, then **180 days** by default.

These are operational/privacy defaults and may move to a dedicated policy config later without changing economic history.

---

# 24. Cross-system anti-inflation rules

1. `NETWORK_GROUP` activity and `CLAN_HOME` activity use separate reward contexts.
2. One source activity cannot be settled twice through unrelated contexts.
3. Creator Network rewards do not automatically fund Clan Treasury.
4. Referral rewards never generate referral rewards.
5. Achievement/medal unlocks do not mint player Points.
6. Treasury spending does not mint direct player Points.
7. Clan item boosts do not directly multiply player-point gross rewards in v1.
8. Bot messages, invalid activity, replayed events, self-referral and prohibited self-creator activity generate zero eligible reward.
9. `SUSPICIOUS` does not automatically equal banned.

---

# 25. Economy health targets

Monitor at minimum:

- P50/P90/P99 points earned per active user/day.
- P50/P90/P99 Season Points.
- Time to Levels 5, 10, 15, 20.
- Mint distribution by source.
- Typing reward/attempt, attempts/day, invalid and suspicious rates.
- Referral verified conversion and tier distribution.
- Creator reward/day, unique speakers and qualified characters.
- Clan Contribution/member, Treasury inflow/spend, HQ progression speed and capacity utilization.
- Captain bonus as a fraction of total mint.

Desired aggregate mint mix is a monitoring target, not a transactional constraint:

| Source | Target share |
|---|---:|
| Daily | 20–25% |
| Typing | 30–35% |
| Missions | 10–15% |
| Clan personal | 10–15% |
| Creator Network | 5–12% |
| Referral | 5–10% |
| Captain bonus | <3% |

If production telemetry materially deviates, publish `GameConfigVersion v2`; never mutate v1 or rewrite historical ledger amounts.

---

# 26. DLX allocation and Base Tap (economy contract 2.0.0)

There is no fixed Point → DLX conversion ratio. XP `RewardLedger` and confirmed DLX allocation accounting are separate. Weekly settlement creates confirmed, auditable, off-chain DLX allocation; it does not make DLX claimable, transferable, tradable, or on-chain and never changes progression or recursively generates rewards. Season 1 price is `UNAVAILABLE`.

The six-epoch policy has a 60,000,000 off-chain DLX allocation-amount base pool and 100,000,000 maximum. Weights total exactly 10,000 bps: Clan Performance 3500 bps, Core Gameplay 2000 bps, Creator / Community Growth 2000 bps, Verified Referral 1500 bps, and Loyalty / Consistency 1000 bps. Current-epoch qualified performance is the basis; detailed scoring formulas are not defined here.

Settlement starts through a Season-scoped audited operational control (initial default epoch 3), not by mutating a published snapshot. Epochs close and freeze independently, delayed settlement runs chronologically against each frozen window, and settled history is immutable. Unused category budget carries to the same category; final residual handling is explicit and audited.

Growth unlock milestones use a configurable qualified-player predicate, never raw Telegram starts/MAU. The milestone list may remain empty until launch values are approved. Each milestone is uniquely keyed, can unlock once, and allocates exact off-chain DLX allocation amount only to still-open epochs without rewriting frozen or settled budgets.

Base Tap awards 1 XP per rewarded tap with an initial 50-tap UTC-day cap, through batched authenticated requests and configurable batch/rate controls. It grants no direct DLX and is ineligible for passive Referral share. PostgreSQL daily aggregation creates at most one canonical XP settlement per player/day. Only accepted requests have terminal, short-lived idempotency receipts; receipt retention is configurable (initial sample: 7 days), expired receipts are safely purged, and rejected/rate-limited/cap-exhausted spam creates no durable receipt rows.

# 27. Phase implementation ownership

The complete economy is specified once, but implemented incrementally:

- Phase 4: `daily`, `missions`, `typing`.
- Phase 5: `referral`, `network_activity`, relevant `anti_fraud.network`.
- Phase 6: `profile_collection`, achievement/medal/collection evaluation that is in S1 scope.
- Phase 7: Clan lifecycle/governance/recovery numeric policies.
- Phase 8: Clan activity, HQ, Treasury, Contribution, inventory/shop, Clan missions and Clan economy.
- Phase 9: ranking config/tie behavior plus real weekly DLX epoch snapshots, growth unlocks, same-category carry-forward, and the confirmed off-chain DLX allocation ledger after Clan economy inputs exist.
- Phase 11: Season-close honors, Typing auto-settle, final reconciliation/freeze.

Each coding agent should read only the economy sections needed by its phase plus this document's global change-control and anti-inflation invariants.

---

# 28. Canonical machine-readable file

`season1.game-config.v1.json` is the initial full snapshot intended to be committed at:

```text
config/examples/season1.game-config.v1.json
```

It is designed to remain compatible with Phase 3's current config approach: `progression` is immediately usable, while later phases incrementally add validation/use for their own sections without inventing values.

When used in production, create a real immutable `GameConfigVersion` row from the reviewed snapshot rather than reading this example file as mutable runtime configuration.
