# B6 security and economic review

The [independent mission lifecycle review](B6_MISSION_LIFECYCLE_REVIEW.md) supersedes the initial activation-state eligibility and adds migration 0019. It documents the confirmed delayed-signal defect, durable source-time intervals, PostgreSQL locking, the owner-approved prospective cutover and unresolved-history holds. No previously settled economics are rewritten.

## Authority and scope

This batch implements the owner's Clan operations request and its Treasury carry/Clan-standing addition on current merged B5 main. It does not redesign frontend routing, publish GameConfig, rebalance any number, implement Season 2, merge or deploy. The owner's B6 instruction explicitly supersedes earlier private invite-DM wording and the old per-settlement truncation policy. Historical documents/config snapshots are preserved, with the supersession documented in the B6 contract.

## Accounting and prospective cutover

Existing `ClanEconomicAccumulator` is uniquely keyed by `(clan_season, config_version)`. New `treasury_remainder` defaults to zero and has PostgreSQL nonnegative and `<10000` constraints. It is updated in the same canonical `settle_clan_reward` transaction as the economic settlement, Treasury credit, Player reward, Contribution/HQ/Score, balance projections and downstream durable events. No Redis economic state or alternative settlement path exists.

For gross `G`, configured split `B` basis points and previous remainder `R`:

```
T, R_next = divmod(R + G * B, 10000)
P = G - T
```

All operands are integers. With `0 <= R < 10000` and `0 <= B <= 10000`, `0 <= T <= G`; therefore `P >= 0` and `P+T=G` for every settlement. Summing a config-scoped sequence gives `10000 * sum(T) + R_final = B * sum(G) + R_initial`. Starting at zero, credited Treasury is exactly floor of aggregate entitlement; the remaining fraction stays durable. At 10%, Gross 2,1,4,3 yields credits 0,0,0,1 and remainders 2000,3000,7000,0. A rollover can legitimately consume the entire Gross unit. The canonical reward primitive permits zero Player net only through an explicit internal treasury-only flag, restricted to positive fully conserved CLAN_HOME activity/mission splits. It still performs its Season, moderation, idempotency, ledger and outbox workflow. Zero net does not change Player progression, points, score-reached time or ranking generation. Generic zero/negative rewards remain rejected. Existing Contribution-before-split, HQ rational remainder and Clan Score calculations are unchanged. Referral share continues to use actual settled Player net; Captain bonus continues to use its existing gross basis and independent limits.

Locking remains economic Season barrier/idempotency claim → ClanSeason row → sorted Player rows → membership → config-scoped accumulator. The shared ClanSeason lock serializes different members' carry consumption. Same-source retry returns the immutable economic settlement before reading/updating carry. Transaction failure rolls back the remainder, ledgers, idempotency record and projections together. Settlement audit metadata records B6_FRACTIONAL_V1, rate, denominator and remainder before/after on economic, reward and Treasury records.

**Exact cutover:** migration adds zero to existing accumulator rows. After old economic workers are stopped/drained and B6 workers own settlement, each newly committed economic source begins contributing under B6. A delayed source not previously committed uses its original pinned published GameConfig and that version's accumulator. Already committed sources return unchanged on replay, regardless of when retried. No legacy settlement is scanned to compute Treasury debt; no existing Player amount, Treasury credit or production balance is changed. Carry is never transferred between GameConfig versions, even at the same rate, or between ClanSeasons. Late old-version work continues only its own accumulator. Subunit balances remain archived at close; the existing earning window, barrier and legitimate drain rules govern whether a source may settle. No extra end-of-Season mint/round-up is introduced.

Preserving legacy fractions would require an additional explicitly owner-approved economic reconciliation/migration. Those fractions were already awarded to Players. Merely seeding old fractions would charge a later source/Player for earlier entitlement; crediting whole legacy units additionally would mint value without conservation against the old rewards. Neither action is authorized or performed. The current migration is structural/default-zero only.

## Missions and operator authorization

Django Admin cannot mutate PlayerMission, ClanMissionProgress, participants, sources or signals. Definitions start disabled; there is no Admin settlement operation. Activation/deactivation uses normal model change permission plus dedicated activation permission, CSRF/admin authentication, actor-bound signed fingerprint with ten-minute validity, explicit confirmation and reason. Forged, expired/stale preview and unauthorized activation tests fail closed. Writes create OperatorAuditEvent plus normal Django Admin history; activations create no rewards or Telegram messages.

Clan drafts restrict requirement selection to the B5 server adapters and validate positive target, ordered times, display title, published config and actual band. Personal validation preserves families/repeat/config binding, adds valid ordered schedules and nonzero integer Telegram identity. Published GameConfig remains immutable. Economic parameters and version identity are guarded by PostgreSQL triggers as well as Admin/model protections; activated or progressed semantics require a new version. Deactivation preserves activation evidence and prior economics. Change-form validation locks the current definition; ordinary edits preserve the latest controlled enabled/activation state, so a stale display edit cannot reactivate/deactivate a definition. Definition locks serialize source recording against Admin transitions; no destructive settlement rewrite occurs. Editable display text does not affect rewards.

## Read privacy, rank integrity and resource bounds

Applicant and Clan standings use the existing Arena sorted-set members/tie ordering and atomic read; PostgreSQL scores and generations plus global pending-event fences prevent plausible-but-stale ranks. No second leaderboard or points-to-rank approximation is added. Nonempty discovery/applicant batches use one Redis read, empty pages none. Explicit rank_status distinguishes missing eligibility from unavailable/reconciling infrastructure. Daily streak is read from canonical claims and reset policy in constant query count. Existing fields and authenticated identity selection remain.

The Clan feed has no mutation route or client-selected Clan. Authenticated own current membership and existing private/no-store response handling are required; outsider and wrong-Clan cursor tests fail. Each source query and merged page is bounded. Cursor is signed, length-limited and scoped to ClanSeason. Only committed canonical achievements/actions are represented. Raw Telegram text/messages, unverified activity, credentials and internal payloads are excluded. Accounting legs of the same reward/purchase are suppressed rather than presented as independent achievements. Event time and nullable recording time are explicit; late-event refresh semantics are documented. Query plans/latency under large production histories remain a measured follow-up, not an unsupported capacity claim.

## Telegram and invite lifecycle

Welcome idempotency uses incoming update identity, with immutable template/body/keyboard snapshots and durable version provenance. Existing ingress authentication/dedupe, first-touch referrals, delivery queue, API admission budgets, retry deadlines and uncertainty semantics remain. Template activation alone never selects an audience or sends. Same-update redelivery cannot reset a failed/ambiguous delivery; a distinct user command can queue another Welcome. Tests use synthetic transports exclusively.

Invite creation no longer queues a Telegram credential send. Dispatch additionally blocks legacy Clan-invite rows before remote HTTP, preserving their audit state as SKIPPED. Stop/drain old workers during rollout: a remote send already in flight cannot be recalled. Authenticated credential retrieval, private access, accepted-versus-active distinction, Telegram verification, expiry/recovery cycles, reservation safety, cancellation/revocation and UNKNOWN/UNCERTAIN/MANUAL_REVIEW handling remain. No public invitation endpoint is added.

Eligible groups and fresh/final verification apply the existing exclusive Network-context rule coherently. Group row locks still fence final concurrent registration; direct Network intent creation is not triggered by Clan add-bot. Group avatars reuse private cache/proxy, strict file-path/MIME/size/pixel sanitization, tokens kept server-side and shared API budgets/refresh slots. API returns only a backend capability URL or null. Storage credentials remain separate from Player avatar credentials.

## Migrations and rollback

- `missions.0003_b6_mission_administration`: activation metadata/permission and definition-history guard.
- `clans.0017_b6_mission_administration`: display/activation metadata, permission and definition-history guard; key field uses slug validation/index with unchanged bounded storage.
- `clans.0019_b6_mission_active_intervals`: durable activation intervals/revision/source links and prospective observation cutoff; uncertain earlier signals remain held, with no historical replay or backfill.
- `clans.0018_b6_treasury_fractional_carry`: zero-default integer remainder and range constraint. No balance/config/settlement backfill.

Apply only in approved staging/production deployment windows. Review rendered SQL and lock behavior on staging first; retain backups. Activation metadata stamps previously enabled definitions as observed enabled at migration time; it does not claim their unknown original activation date or change rewards/progress. Test the migration graph/drift before workers start. Stop old invitation/economic workers before new behavior owns delivery/settlement. No production migrations have been run by this task.

Safe rollback disables new mission creation/activation and pauses new economic settlement/invite delivery while preserving B6 code for pending obligations. Keep the carry-aware settlement implementation: returning to legacy truncation silently loses prospective fractions, and old workers can send queued invite credentials. Do not drop accumulator/history columns or replay/delete ledgers. Forward code correction or owner-approved revert retaining accounting/privacy fixes is safer than schema downgrade. Welcome rollback can select an older validated template; queued snapshots remain fixed.

## Validation and residual gates

Exact commands, results, runtime SHA and hosted check links are recorded in the validation evidence/PR handoff. Full PostgreSQL regression includes bootstrap/authentication, Daily/personal Missions/Typing/referrals, Network/Speaker, Clan core/invites/governance/recovery, Treasury/Inventory/HQ, Arena, notifications/Telegram and Admin/config publication. New cases cover Admin-only Group/Individual creation and canonical auto-settlement, source ingestion/windows, projection batch/failure behavior, feed privacy/pagination, repeated starts and invite delivery removal, plus Treasury carry/retry/concurrency/version/rollback/history boundaries.

No production test rewards, live Telegram messages/broadcasts, frontend edits, merges or deployments are performed. PH-037 branch rules and PH-043 product evidence remain owner release gates. Frontend acknowledgement/pin, paired staging/device acceptance, production storage/cache configuration and realistic load measurements are pending independent operational evidence. Green backend CI does not establish device acceptance or authorize release.
