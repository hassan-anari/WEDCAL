# Verification record and full-scope completion matrix

The Growth/Admin addendum is part of the original Phase 4 scope. This review package does not
declare Phase 4 complete or authorize Production. Functional implementation and synthetic capacity
evidence are distinct. Required CI results and the exact final SHA are reported on the Draft PR.

## PostgreSQL evidence

Local full regression run: **1,296 passed, 4 opt-in skips**, 511.43 seconds, PostgreSQL 17.11.
That run preceded the last topology-conflict, completed-worker skip and final late-correction tests.
The final focused run then passed **34 tests** in 76.90 seconds, covering configurable profiles,
Growth addendum and Admin. The empty isolated schema rollback/forward smoke separately passed
(1 test, 36.36 seconds). Routine CI runs the entire suite plus the isolated migration smoke;
the large scale probes remain explicitly opt-in. No existing security assertion was weakened.
The expanded Growth suite additionally passed 13 tests in 50.18 seconds, including four real
closed-Season conservation cases: two/six Epochs with first/final-cutoff Growth. Admin Carry and
policy-inspection refinements are covered by the final CI run as well as local Admin tests.

Reproduce routine tests with `pytest`, configured for isolated PostgreSQL/Redis/RabbitMQ as in CI.
For the empty migration cycle use POSTGRES_DB=phase4_migration_local and
PHASE4_MIGRATION_CYCLE=1 with the dedicated migration test only. It refuses a differently named DB.

Full lifecycle simulations use configuration values, not profile-specific application branches:

| Profile | Gate | Base | Growth | Confirmed allocation | Final residual | Closed |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2×7 + 1 closing day | 1 | 100,003 | 0 | 29 | 99,974 | yes |
| 6×7 + 3 closing days | 3 | 100,003 | 0 | 24 | 99,979 | yes |
| 3×5 + 2 closing days | 1 | 100,003 | 0 | 29 | 99,974 | yes |
| 8×3 + 2 closing days | 1 | 100,003 | 0 | 24 | 99,979 | yes |

These deliberately small budgets test exact integer remainder behavior; the separate official
example retains 60M/100M, and the addendum tests use 3M/5M/20/2M. The five canonical categories
also run through automatic orchestration in two- and six-Epoch profiles with actual producer data.
The full-close simulation proves no weekly wallet auto-claims and vested Typing XP auto-settlement
only at Close. The one-day final-Epoch late-source test proves funded correction and final residual
conservation without editing original allocations. See checked-in simulation JSON and CI artifacts.

Required Growth A/B scenarios pass: first-cutoff funding splits 2M across both Epochs; first
achievement at final cutoff funds the final Epoch with all 2M before Freeze. Below/exact/above,
distinct multi-Epoch cohorts, pending/revoked users, concurrent Freeze, retry, failed transaction,
multiple milestones, exact remainders and no-receiver zero funding are covered on PostgreSQL.
Admin tests prove registration, model/overview permissions, safe evidence projection, no POST
mutation/actions/inlines, canonical totals and bounded pagination/query count with 201 history rows.
The final-code direct million-registration normalized-receipt probe passed in 694.16 seconds;
economic service time was 544.735 seconds with 100k active/qualified participants. This remains
synthetic boundary evidence, with the capacity limitations recorded separately.

## Every original requirement and both addenda

| Requirement | State and evidence | Remaining boundary |
| --- | --- | --- |
| 1 Preserve Phase 3 gameplay/economics | Implemented; full regression, all five categories, corrections/anti-abuse retained | final CI required on PR head |
| 2 Authoritative GameConfig/policy ownership | Implemented/PG verified; schema 4 explicit checksum selection, publication/binding validation, independent policy approval, DB guards | operational approval must be real, never an example label |
| 3 Generic timing/topology/closing | Implemented/verified 2/3/6/8 Epochs, multiple day lengths, no closing Epoch, historical parsers preserved | FROCAL must consume additive timing contract separately |
| 4 Configurable budgets and immutable commitments | Implemented/verified arbitrary valid integers, exact base/category remainders, signed-bigint boundary | storage/authorization bounds remain structural |
| 5 Complete Growth Unlock | Implemented/PG verified; authoritative STD count, unique immutable event/placements, reserve/cap integration | live configurations and activation absent by design |
| 6 Weekly automatic lifecycle | Implemented/verified explicit authority, chronology/gate, restart/concurrency/pause, source blockers, stage/status metrics | external alert routing and deployed producer compatibility require operational integration; not configured here |
| 7 Repository hardcoded-value audit | Inventory plus domain ownership matrix implemented; known accidental structure/budget assumptions fixed | mechanical classifications are not a formally exhaustive semantic proof of every literal/string; independent audit remains necessary |
| 8 Clan Mission semantics | Preserved; pinned band amounts, compatible config, immutable definitions; existing Individual/Group lifecycle regressions | no band migration/repricing feature added |
| 9 Safe mid-Season tuning | Implemented/verified Daily/Typing in-flight cutovers and historical ledger attribution; active setup mutation rejected | existing gameplay-specific publication restrictions retained |
| 10 Capacity up to 1M registered | **Partially validated under synthetic load**; direct normalized 1M/100k-active run, increasing scales and activity mixes, plans/RSS/CPU/counters | full producers, concurrent traffic/SLA, parallel throughput, large Close/corrections, isolated WAL/backlog not validated; see CAPACITY.md |
| 11 End-to-end PostgreSQL matrix | Core functional matrix implemented/verified, real process death and transaction races, extra profiles, late correction, migration cycle | massive end-to-end real-source/traffic load remains open; this prevents full capacity certification |
| 12 Production boundaries | Preserved; disposable databases only, default-off workers, no live config/Season/policy writes | deployment/activation/reset/live settlement/merge prohibited |
| 13 Review package | Architecture, audit, schemas/examples, Growth proof, runbook, migration review, simulations, regressions, performance artifacts, limits | exact SHA/CI links in Draft PR; independent owner/reviewer review pending |
| Growth addendum 1.1–1.6 | Implemented/PG verified generic pre-Freeze recognition, STD distinct count, final-Epoch funding, A–E, atomic exact placement | no additional unapproved economic recognition rule introduced |
| Admin addendum 2.1–2.4 | Implemented/PG verified all listed models plus facts/readiness/new audit records, protected summaries, redaction, no mutation | list performance measured at 201 rows; million-row Admin latency/history-heavy plans still unmeasured |
| Addendum testing/delivery | Growth/Admin tests, worked examples, locking/conservation, sample reports and required CI included | unmeasured load items explicitly open; no declaration of complete Phase 4 |
| Additional owner clarification | Official gate/history/no-milestone zero behavior preserved; full original scope retained | no competing PR or Demo-only engine |

## Review and activation decisions

The revised generic Growth rule is specified by the owner's addendum; the old proposal is superseded,
not awaiting approval as the active implementation. No further economic rule was invented. Before
any live use, the owner must approve actual Season/config/policy documents and separately authorize
deployment/automation; all are outside this PR. Capacity acceptance and external alert integration
remain operational gates. No historical real-user data, frozen budget or confirmed allocation was
modified. This Draft PR can be reviewed while those limits remain explicit; it is not a readiness
certificate or a request to merge/deploy.
