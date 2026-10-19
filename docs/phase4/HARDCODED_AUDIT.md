# Numeric ownership audit

Baseline: `9b15febb1572910bf97f22800f59767d61ce9e85`. Python numeric literals across backend
services/models/validators/tasks/APIs/settings and migration SQL, plus numeric example inputs, are
inventoried in [numeric-inventory.csv](numeric-inventory.csv). Reproduce with
`python scripts/audit_config_numbers.py --output work/numeric-inventory.csv`.
The inventory is deliberately mechanical: `REVIEW_CONTEXT`/`ARITHMETIC_OR_BOUND` classifications
are not a proof of business ownership. This table records the domain interpretation and changes.
Historical documentation and JSON fixtures retain their labeled Season-1 examples; the new architecture
and runbook supersede their fixed-topology guidance for new Seasons. No historical migration was edited.

Locations below identify functions and original baseline lines where relevant; inventory lines refer
to the final checkout. Security/storage/protocol invariants remain in code, not editable policy JSON.

| Finding / location | Original value and reason | Classification / authoritative field | Pin | Change / regression evidence |
| --- | --- | --- | --- | --- |
| game_config/services.py `parse_economy_sync` (1051–1056) | 6×7, 45, 3: Season-1 preset enforced globally | business topology; `dlx_distribution.timing.*` | Season | schema 4 generalizes equation; old parsers retain history; `test_profiles_automatic_full_lifecycle_exact_residual` (2/6/3/8 Epochs) |
| game_config/services.py `preview_safe_cutover` (64) | 7-day modulo: old prospective DLX rule | business setup must be immutable; baseline distribution | Season | ordinary republish cannot change active commitments even at boundary; `test_active_season_dlx_policy_requires_pinned_setup` |
| arena/models.py / migrations/0001_initial.py (507) | Epoch number ≤6 | accidental product bound; baseline count | Epoch | new migration removes fixed upper bound; new trigger validates baseline count, dates and base commitment; 8-Epoch regression |
| seasons/close.py `_epochs`, `reconcile_season_dlx` (277,452,460,497,545) | 6 settled Epochs, final number 6, no seventh | business topology; baseline count | Season | terminal/carry/residual/batch totals use count; 2/6/3/8 full reconciliation tests |
| arena/management/.../epoch_freeze_preflight.py (15) | CLI choices 1..6 | business topology; selected Season Epochs | request | removed fixed range; existing DB lookup validates existence |
| arena/policy.py (100) | policy.days=7 for legacy implementation | versioned economic denominator | policy | independent CONFIGURABLE_V1 permits approved days; binding requires exact structural match; 3- and 5-day Epoch tests |
| base_tap/v2.py (312) | fallback Epoch days=7, using burst config's structural field | accidental structure fallback; Season baseline | Season / burst reward config separately | remove fallback, resolve baseline and fail closed without setup; existing lease/boundary tests |
| arena/epochs.py `_snapshot_budgets` (257) | Growth rejected; category Growth floors lost units | missing business path; baseline pool + bound Growth rule | Epoch Freeze | exact final-category remainder, all fresh funding included; `test_growth_is_exact_future_funding_and_retry_safe` |
| Base/max/Growth capacity | 60M/100M/40M in docs/examples | explicit example inputs; pool base/max | Season | no service constant; capacity derived as max−base; tests 100003/200019 and signed-bigint limit |
| arena/epochs.py, corrections.py allocation/cap calls | base-only fresh denominator | economic input; base+Growth placements | Epoch | normal allocation and correction cohort use combined fresh funding; Growth reserve/cap tests |
| arena/migrations/0015 /0016 reserve/cap functions | base-only bigint multiplication | funded-policy formulas, not editable invariant | original Epoch | new 0022 replaces function bodies with combined fresh funding and numeric intermediates; no historical reprice; max-bigint test |
| arena/epochs.py `evaluate_growth_unlocks` (598) | always unconfigured when reached | intentionally missing path | baseline milestones + policy rule | durable once-only events/placements; zero/one/multiple/retry/concurrency/last-window tests |
| arena/tasks.py `settle_epochs` | unconditional 0 | deliberately unimplemented | explicit automation control | orchestration installed; no control → no scheduled economic writes; pause/old-worker/process-death tests |
| policy weights, saturation, qualification, participation, minimum score, 1% cap, 25% Carry release, 5% reserve | approved Phase-3 policy example numbers | independent economic policy fields | immutable binding | values unchanged; full Phase-3 scoring/security tests; new policy references explicit |
| five categories; 10000 bps; 0/1 identities; exact remainder | canonical identities and arithmetic | structural invariants | code/database | retained; allocation/reconciliation/overflow tests |
| Typing calculations.py 12000; display 50/100 | milliseconds/minute ÷ five characters/word; half-up display rounding | unit definition/display invariant | code | retained; accuracy/WPM tests. Actual reward brackets/quota/max remain config |
| achievements seven-day=7, thirty-day=30, first-*=1 | named identity semantics, never payouts | canonical achievement identities; configured targets validated for those names | entitlement | retained; achievements mint zero XP by contract. Season honor thresholds remain baseline config |
| progression, Daily, Typing, Tap XP and limits, personal Mission rewards | numeric values in labeled published example payloads | gameplay GameConfig namespaces | effective source/attempt/burst/verified entitlement | no service reward rates changed; midseason in-flight/Daily/wallet attribution tests and existing regressions |
| Referral activation bonuses/Level/age/tiers, passive shares | existing config-driven amounts | `referral.*` and bound qualification policy | verified acquisition/source time | retained; actual acquisition isolated from derived passive credit; Phase-3 referral tests |
| Creator rewards / speaker zero / activity thresholds | existing config-driven rates/anti-abuse | `network_activity.*`, `anti_fraud.network.*` | source/window config | retained; canonical owner/speaker anti-duplication tests |
| Clan rewards, Treasury, contribution mapping, Captain bonus, HQ/capacity, Shop prices/durations | existing configuration-driven values | `clan.*`, `anti_fraud.clan.*` | ClanSeason/settlement/purchase/effect | retained; canonical Clan economy and inventory tests |
| Clan Mission SMALL/MEDIUM/LARGE/GROUP | band IDs; amounts already taken from pinned config | definition selection + `clan.missions.reward_bands` | definition and compatible ClanSeason | no hardcoded reward amounts found; no migration/repricing; existing Individual/Group lifecycle tests |
| cleanup/retention | message/fingerprint/evidence/receipt horizons | existing GameConfig retention + explicit runtime ingress retention | source/effective config and audited evidence hold | no shortened horizons; durable recognition proofs survive purge; existing retention/correction regressions |
| 32767 / 2^63−1 / Tap maximum burst 1000, lease 300, retention ceiling 30 | platform storage/authorization bounds | structural/security invariants | code/schema | retained; configured values remain bounded, not arbitrary JSON authority |
| HTTP statuses, string lengths, cryptographic sizes, Redis key arity and lock namespaces | protocol/schema/security definitions | code/database | structural | retained; not economy parameters |
| worker batches, retry counts/backoff, page limits, connection timeouts, queue scans | operational bounds/defaults | runtime settings or bounded service parameters | process/request | inventoried; new economic job has separate configurable scan/batch/time limits. Fixed bounded legacy defaults are not reward rates |

## Remaining review scope

The mechanical inventory includes source numbers, not a formal semantic proof about every possible
number encoded in arbitrary string content. Documentation/sample values do not execute economics.
Legacy disabled fallback configs remain parseable and retain original behavior. A fresh
[semantic source pass](SEMANTIC_AUDIT.md) now verifies the requested economic/gameplay domains and
records the unsupported Daily lifecycle publication fix. Read-only FROCAL inspection found no
fixed 6/42/45 remote Season math; its parser strips additive timing and its UI lacks an earning/
closing distinction. The exact dependency and compatibility evidence are in [the handoff](FROCAL_HANDOFF.md).
No FROCAL source was changed.

Million-user operational bounds require contention and queue-load validation beyond a single
normalized-receipt probe. No statement here certifies Production sizing or full throughput readiness.
