# Season 1 completion — Phase 2 gameplay handoff

Repository: `hassan-anari/WEDCAL`. Base: `e11bdb3` (Phase 1 Freeze safety, PR #35).
Branch: `ai/season1-phase2-gameplay-completion`. Delivery: one unmerged Draft PR;
independent review and owner approval precede merge, deployment or Phase 3.
Frontend reviewed read-only: FROCAL `eb76307615322016bc39509c467f64be0268423a`.
Exact revisions/check links belong to the Draft PR and accompanying validation evidence.

## Approved decisions and implemented scope

The owner's finalized instruction supersedes every absolute “Top 10 Players” reference
in the original Phase 2 brief. `season-top-10` remains final top 10% / 1000 bps;
`season-top-1` remains final top 1% / 100 bps; Champion remains rank #1. Integer
inclusion is `rank * 10000 <= ranked_population * configured_percentile_bps`.
No minimum-one exception: populations below 10 receive no Top 10% award; populations
below 100 receive no Top 1% award. Champion still requires a ranked player.
Ranking order, frozen evidence, configuration, Medal keys and frontend copy/assets stay intact.
No ranking configuration migration or frontend change is required.

The owner additionally confirmed cumulative ACTIVE membership time for Clan Loyalty:
complete elapsed 24-hour periods in the same Clan and Season, excluding access-loss
gaps and merging overlaps. Rejoining resumes accumulation for that ClanSeason;
another Clan or Season cannot contribute. This is not a login/message/calendar-day rule.

Implemented: controlled official external mission availability; missing career Medal
reconciliation; independently reproduced mission transition and ranking selection fixes;
isolated seven-item equipment/Trophy Hall acceptance and regression evidence.
No economic rebalance, source-policy change, automatic backfill or production operation.

## External mission: configuration before activation

`read-season-brief` uses existing `TIMED_EXTERNAL`, server-owned opened_at, token binding,
configured dwell, `REWARD_READY`, and canonical claim. Its approved destination is exactly
`https://dexoland.dalexio.com/`. HTTP, another host/path/query or a lookalike host is refused
for this mission. Generic mission URL validation and unsupported verifier safeguards remain.
Start retries return the original opening time/token. Dwell/token establish a timed interaction,
not proof of reading. Verification creates no Points; claim uses the preserved config/reward/time.

The old unconditional unavailable marker is removed only for a policy-bound official external
definition. Legacy INTERNAL definitions remain unavailable. Admin preview/activation validates the
same destination/policy; no new activation bypass or reward endpoint exists.
Two reproduced defects were also fixed: disabled effective missions could still verify an
in-progress row; deactivation between verification inspection and the locked ready transition
could still create an entitlement. New verification now fails closed. Already REWARD_READY claims
retain the existing disabled/replaced-definition settlement behavior.

Approved operator sequence, after code review and an authorized deployment:

1. Read the actual effective published GameConfig and all existing versions/progress for this key.
   The owner-supplied historical Production observations and repository examples do not establish
   today's published policy. This task did not connect to Production.
2. If the current effective mission rule is INTERNAL, clone the **actual current full payload** into
   a new draft GameConfig with a new version and reviewed effective time. Change only the
   `missions.initial_definitions` entry identified by `id == "read-season-brief"` so its
   `verification` is `TIMED_EXTERNAL`. Keep its reward, `ONCE_PER_DEFINITION_VERSION`, dwell,
   every ranking rule, Tap/DLX namespace and all other values exactly as observed.
   Do not republish an example over live policy. If policy already matches, no duplicate publication
   is necessary. Use existing GameConfig validate/preview/publish permissions and reason/audit.
3. Create a disabled MissionDefinition draft using the same key and a fresh unused version if the
   old definition is activated/progressed. Family `TIMED_EXTERNAL`, repeat
   `ONCE_PER_DEFINITION_VERSION`, parameters
   `{"destination_url":"https://dexoland.dalexio.com/"}`. Do not alter protected historical semantics.
4. Once matching GameConfig is effective, preview the new draft in existing Mission Admin.
   Verify actual configured XP and destination, then explicitly activate using the signed
   actor-bound confirmation/reason. Deactivate the old definition through the same workflow.
   A brief fail-closed cutover gap is preferable to inconsistent enabled semantics. Keep an enabled
   replacement for the same key so previously ready claims remain reachable by the existing UI.
5. In approved isolated staging, authenticate a test player, start/open the official site, return
   after configured dwell, verify and claim. Repeat start/verify/claim and inspect one canonical
   MISSION ledger row, PlayerSeason and lifetime projections. A definition version allows one claim
   for that version; it never rewrites an earlier claim. No live economic QA is authorized here.

No data migration publishes config or activates definitions. Rollback: deactivate the new mission,
preserve ready obligations and the claim-compatible code, then use a forward correction or another
reviewed future GameConfig. Never rewrite/delete published policy, progress or claimed rewards.
FROCAL `useB3.ts` already opens returned destinations and obtains a token again before verification;
EarnPage renders the generic available/in-progress/reward-ready/claimed states. No source change is
needed, but final backend pin plus paired staging/Telegram website-open acceptance remain separate.

## Medal acquisition and authoritative evidence

All medals grant **zero Points and zero DLX**. Career awards reconcile on authenticated Profile reads
and customization using the existing Player lock and uniqueness constraint. They are not background
reward settlements. Season medals are evaluated by Season Close after the final ranking freeze.

| Medal | Rule / acquisition path | Authoritative evidence |
|---|---|---|
| season-founding | Participation, Season Close | Existing positive-score final standings participation policy |
| season-participant | At least 5000 Season Points, Season Close | Frozen final score and pinned Season baseline |
| season-veteran | At least 30000 Season Points, Season Close | Same |
| season-top-10 | Final top 10%, Season Close | Frozen rank/population; integer percentile boundary |
| season-top-1 | Final top 1%, Season Close | Same, 100 bps |
| season-champion | Final rank #1, Season Close | Frozen final rank |
| activity-veteran | 1000 verified events, Profile reconciliation | VALID, program-active NetworkActivity |
| clan-loyalty | 30 complete cumulative ACTIVE days, Profile reconciliation | Membership start/end, activation/access events; unioned same-ClanSeason intervals |
| leadership | Captain and Clan HQ Level 5, Profile reconciliation | Canonical Captain identity/membership and historical HQ XP ledger, source config/threshold/time |
| mission-master | 50 claimed missions, Profile reconciliation | Durable CLAIMED PlayerMission records |

Clan Loyalty intersects membership time with Clan creation, Season bounds, departure and dissolution.
No current role flag or last-login fallback establishes missing activation history. Access loss pauses
new time; legitimately earned historical time remains. Overlapping/rejoined intervals are unioned,
never summed twice. Historical departure is not an entitlement revocation.

Leadership reconstructs HQ XP in source-time order from ledger entries, using the configured HQ curve
at the source. A current `hq_level=5` projection alone is insufficient. It requires a qualifying Captain
membership interval, excludes known authority/bot loss and unresolved recovery, and can recognize a
later valid restoration while HQ is already at Level 5. A valid earlier qualifying interval remains
provable after departure/dissolution. No current Soldier, selected role or unrelated Captain qualifies.
Missing/unsupported historical evidence fails closed; this task does not fabricate it.

New career award evidence records qualifying membership IDs, ClanSeason/Season, interval union and
threshold, or qualifying timestamp/HQ entry/config/threshold. Existing award identities, dates and
provenance are preserved. `source_season=null` retains career-once identity; evidence identifies its
qualifying Season. Existing Profile/Collection response schemas remain unchanged.

Ranking defect reproduced before fixing: Django's multi-relation `exclude` could match a founding
award from one Season and a different award from the closing Season. The player was skipped for all
closing-Season honors. The selector now uses a subquery requiring founding key and source Season
on the same award. No percentile/tie formula changed. Real stage-transaction failure tests preserve
committed earlier batches, roll back the interrupted batch, resume at AWARDING_HONORS and retain
immutable standings and existing awards.

## Achievement audit

| S1 Achievement | Existing server-owned evidence | Result |
|---|---|---|
| First Mission | CLAIMED PlayerMission count >= 1 | Preserved |
| First Referral | VERIFIED ReferralRelation count >= 1 | Preserved |
| Seven / Thirty Day Streak | Maximum canonical DailyClaim streak >= 7 / 30 | Preserved after later missed days |
| First Verified Activity | VALID, program-active NetworkActivity count >= 1 | Preserved; scope ambiguity below |
| Clan Ready | Derived persistent progression reaches configured Clan gate | Preserved |
| First Clan | Canonical ClanMembership created by verified activation workflow | Preserved |
| First Contribution | Canonical ClanContributionEntry total >= 1 | Preserved, gross-before-split evidence |
| First War / First Victory | Unavailable in S1 | Remain unavailable |

Unlocks are durable/unique; customization rejects another player's or locked entitlement. No
Achievement mints Points, removes earned history on departure, or uses frontend demo state.
First Verified Activity's contract says verified activity without explicitly extending the existing
Network-only producer to Clan Home/Tap/all Telegram messages. No expansion is made in Phase 2.
Any broader counting policy requires an explicit decision and independently attributable source data.
Moderation/reversal behavior remains existing auditable history: no retrospective deletion is added.
PH-044 multi-season same-key display provenance remains a Season 2 API/product follow-up.

## Clan equipment and Trophy Hall acceptance

| Item | Existing price | Existing effect |
|---|---:|---|
| rally-beacon | 2400 | HQ XP +20%, 120 minutes |
| contribution-booster | 2800 | Contribution +15%, 120 minutes |
| mission-progress-booster | 3100 | Clan mission progress +20%, 90 minutes |
| coordination-booster | 3300 | HQ XP and mission progress +10%, 90 minutes |
| capacity-expansion-module | 4100 | Season capacity +5, maximum 2 uses |
| roster-slot-module | 3800 | Season capacity +1, maximum 5 uses |
| command-relay | 5200 | HQ XP +2000, maximum 2 uses |

These are validated published **test snapshot** values, not an assertion that Production has this
exact payload today. Effective published GameConfig owns operational values.
No item price/formula/catalog or Clan economy code changed. Highest bonus per category remains
effective; booster effects do not multiply gross Player rewards. Expiration is exclusive at server
`expires_at`. Purchase debits Treasury/adds quantity; activation consumes one quantity and records a
use/effects. Captain authority, account/Clan/Season integrity, affordability, inventory, use and
capacity limits remain server-owned. Dissolution/recovery cannot authorize new use.

United Front unlocks at 25000 historical Season Contribution. Treasury Keeper unlocks at 20000
historical Treasury **earned**, independent of spending. Threshold-minus-one/exact, replay, durable
ownership, zero reward and API tests preserve these rules. Banner Raised remains Season 2 deferred;
Defence/Alliance/World Event items remain unavailable. Existing B6 fractional carry/source-time
accounting and Season-close protections are unchanged.

Safe reproducible QA, in isolated PostgreSQL with normal test factories:

```bash
uv run pytest apps/clans/tests/test_completion_equipment_qa.py -q
```

The scenario creates a synthetic eligible Captain/Clan, explicitly TEST_ONLY canonical economic
settlement evidence, then invokes real authenticated HTTP purchase/activation and canonical
effect/mission-progress services. It checks debit, quantity, retry, correct accounting leg, expiration
and frontend-compatible equipment state for all seven items. Machine-readable observations are
written to `work/phase2-equipment-*.json`. The funding amount and supplied domain activity are test
fixtures, not claims of organic activity or Production acceptance. No debug route, Treasury grant,
management-command economy shortcut, live player mutation or integrity-trigger bypass is introduced.

Concurrency regression covers duplicate/different purchase keys, no overspend, duplicate/different
activation keys and single-quantity double-use prevention, using real PostgreSQL connections.
Existing lifecycle, season-history, B6 carry, mission signal and contract tests remain required.
For staging, run this test on disposable isolated services; any manual seeded staging scenario must
be explicitly labelled test-only. Production acceptance needs separate owner approval and a plan
using legitimate retained evidence; do not insert arbitrary Treasury or disable integrity guards.

## Migration, release and outstanding gates

Only new migration: `profile_collection.0002_career_medal_evidence`, an additive JSON evidence column
with empty-object defaults. No config publication, award replay, historical ranking/reward rewrite or
economic balance backfill. Inspect its SQL/locks on staging before an approved rollout. Retain the
column and earned awards on rollback; reversing it would discard new audit evidence.

No API fields, endpoint paths, OpenAPI fixtures, FROCAL assets or frontend source changed. No new
economic config is needed for percentage medals or career awarding. Mission conversion alone needs
a matching effective-dated config when the observed published rule is still INTERNAL.

P1 release gates: final hosted checks/independent review, operator inspection of actual current
mission configuration, approved mission publication/activation, and final-pin paired staging/device
acceptance. First Verified Activity broader-source interpretation is an explicit policy follow-up,
not an invented eligibility expansion. PH-037 branch rules remain repository-owner controlled.

Phase 1 remains intact: `APPROVED_EVALUATORS` stays empty; no Epoch is forced frozen, qualification
decision fabricated or DLX settlement enabled. Phase 3 owns qualification/scoring/source-completion,
weekly settlement activation and any future DLX work. No Phase 3 implementation is included.
Draft PR stays unmerged. No Production mutation/deployment, fake Production rewards/balances,
DLX activation, automatic backfill or unauthorized frontend change was performed.
