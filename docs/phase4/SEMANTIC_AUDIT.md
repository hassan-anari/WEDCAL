# Semantic configuration review

This is a fresh source-level pass, independent of the numeric inventory's heuristic labels. The
review followed actual value resolution, pinning and settlement paths, then compared them with the
approved examples and targeted PostgreSQL regressions. It is an author-side review for the requested
domains, not a claim that an independent human reviewer has approved the PR or that arbitrary code
strings have been formally proved safe. Independent PR review remains the normal delivery gate.

## Findings and ownership

| Source location / value examined | Semantic classification and authority | Pinning / disposition | Regression evidence |
| --- | --- | --- | --- |
| `game_config/services.py:parse_economy_sync`; legacy 6,7,45,3 | Historical schema-2/3 compatibility restrictions; schema 4 timing is configuration | baseline Season; preserve historical parser, no Demo branch | 2/3/6/8-Epoch lifecycle, variable Epoch lengths |
| `arena/epochs.py:initialize_epochs`; floor/remainder and earliest +1 | Exact integer division, not a budget choice; base pool and count from baseline | Epoch creation, conflicting existing commitments rejected | generic profile and draft-conflict tests, signed-bigint boundary |
| `arena/growth.py:unlock_growth`; threshold, amount, equal split | Milestone key/count/amount from baseline; recognition/placement from approved bound policy | unique immutable event + receiving records under Season/Freeze barrier | first/final Demo cutoff, 19/20/21, concurrency/retry, multiple milestones |
| `arena/configuration.py:validate_policy_compatibility` | No duplicated budget or milestone authority in policy; exact reference/checksum/days compatibility | immutable publication/binding | registration-to-activation full 15/45-day simulations |
| `arena/policy.py` / `policy_registration.py`; weights, age, scoring, participation, caps, reserves | Known numeric fields validated, all actual business values from immutable policy; 10000 is bps denominator | policy approval and per-Epoch binding | strict schema, invalid weight/cap/boolean rejection, original Phase-3 scoring tests |
| `arena/scoring.py:core_score/clan_score/creator_scores/emission_limit` | Formula identity/unit conversion only; day count, weights, saturation, minimum score, targets and cap resolved through policy | frozen performance/budget snapshots | all-five-source lifecycle and policy-scoring regressions |
| `arena/corrections.py:finalize_correction_round`; fresh funding, cap and reserve | Bound policy applied to original frozen base+Growth; no unfunded correction path | immutable round/entitlement/payment | final-Epoch late-source funded correction; unchanged original allocations |
| `daily/services.py:get_daily_state/claim_daily`; offsets 0/1, modulo | Array indexing and cycle arithmetic; reward cycle and reset from effective config | DailyClaim and XP ledger config FK | prospective cutover, atomic duplicate/reward tests |
| `game_config/services.py:publish_game_config`; Daily lifecycle booleans | **Verified gap:** unsupported reset/catch-up/cycle switches were accepted even though runtime did not implement them | now reject unsupported new publication; historical parsing/rows retained | four unsupported-flag regression cases; normal rewards unchanged |
| `typing_game/calculations.py`; 12000,10000,50,100 | WPM unit definition, bps and display rounding; actual XP brackets/quota/caps from TypingConfig | attempt/CREDIT original config, wallet claims retain attribution | bracket calculation, in-flight cutover, original-config wallet claims |
| `base_tap/v2.py`; reward multiplication and timing | points_per_rewarded_tap, burst/quota/cooldown/lease from immutable lease config; Season topology from baseline | lease and accepted transaction | existing lease/replay/cutover tests; no daily cap reinstated |
| `missions/services.py:verify`; `rule.reward_points` | versioned personal Mission definition/config, no hidden reward band | verified entitlement, later claim does not reprice | Mission verification and original Phase-3 source-time tests |
| `clans/missions.py:record_mission_progress`; band selection, 10000 | Reward amounts resolved from definition's compatible published config; 10000 is fixed effect denominator | definition + ClanSeason compatibility; completed semantics immutable | custom SMALL=211/37, MEDIUM=307/43, LARGE=401/59, GROUP=503/61 **test-only** inputs; every participant/HQ payment exactly once |
| `clans/economy.py:settle_clan_reward/purchase_item`; price/splits/effects | Treasury, Contribution, HQ, Captain coefficients and Shop price/duration from config; integer remainders structural | canonical transaction/purchase/effect config | existing Clan economy/purchase/concurrency/cutover regressions |
| `referrals/services.py:activation_eligibility`; min age/level, fallback policy version 1 | Approved config and bound qualification policy; version 1 is legacy unbound identity fallback, not a reward rate | verified acquisition/source config; derived shares excluded from DLX | verified-referral isolation and full-five-category source tests |
| `network/services.py:settle_window`; chars/points/daily cap | all rates, limits and anti-abuse from source/window config | immutable canonical source facts | Creator owner/speaker anti-duplication, source retention and late-source regressions |
| `progression/services.py:calculate_progression` | Level/Dexo thresholds from effective versioned config; no service level curve | ledger immutable; prospective read projection references effective config | existing progression/boundary tests |
| achievements seven-day=7, thirty-day=30; medal 1000/100 examples | Named achievement identities retain their meaning; Season honor thresholds/ranks are baseline inputs | immutable entitlement/final standing | existing achievements/ranking/Close regressions; no medal rebalance |
| new alert 900/3900/3/3600 defaults and page/workload/retry limits | Runtime operational controls, not economic inputs; configurable env/rules/routes | process config; per-event route snapshot pinned | threshold/severity/dedup/retry tests; mocked Telegram transport only |
| storage lengths, integer bounds, lock namespaces, category identities, denominator, uniqueness | Structural/security invariants; remain code/DB enforced | schema/transaction invariant | DB immutability/conservation/compatibility/concurrency tests |

The custom-band regression creates and activates new definitions in an active Season using a
compatible published config. It does not reprice existing ClanSeasons or migrate old band rewards.
The new policy workflow accepts only one authoritative shape and independently registers it; it
does not move source authenticity or conservation rules into editable JSON.

Approved historical example payloads and the existing scoring/allocation/correction formulas were
not rebalanced in this continuation. Gameplay rates differ only in explicitly synthetic test inputs.
The only newly verified configuration gap was unsupported Daily lifecycle publication; it is rejected
before mutation, preserving the approved runtime behavior. No additional unintended numeric business
input was found in the reviewed domains. Per-file/line numeric provenance is regenerated in
`numeric-inventory.csv`; the earlier domain matrix retains the Phase-4 baseline findings/fixes.

Source-fanout, massive history and infrastructure workload sizing remain separate measured-capacity
questions. Their larger benchmarks are explicitly deferred by the owner's final sequencing instruction.
