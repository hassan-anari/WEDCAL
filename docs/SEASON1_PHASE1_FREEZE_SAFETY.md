# Season 1 Phase 1: protective Epoch freeze boundary

Scope: backend freeze safety only. No scoring, DLX allocations, growth unlocks, eligibility thresholds,
frontend work, historical backfill, production operation, merge, or deployment. Base: `7a9f460378ce27c927d90de3ec1bf7e9d5a1bfaf`.

## Finding and approved boundary

On the base revision an ended Epoch with no qualification decisions and no pending providers
successfully becomes FROZEN, with five category budgets, zero player snapshots and final timestamps.
`test_missing_qualification_completion_cannot_freeze` was executed before the implementation and
failed with `DID NOT RAISE EpochBlocked` on isolated PostgreSQL 17. This is missing evidence, not
proof of an empty or ineligible player population. No production database was inspected by this task;
the owner's earlier six OPEN Epoch observations do not establish today's state.

Source of Truth §57 and Economy Contract §26 define six weekly windows and versioned qualification,
but do **not** define `QUALIFIED_PLAYER / 1` eligibility criteria, complete evaluation population,
retrospective decision authority, or a Telegram source-completion watermark. GameConfig's predicate
contains an identifier/version; publication and immutable JSON do not implement that policy.
Historical Phase-9/11 PASS evidence does not cover this newly reproduced condition.

This PR implements the explicitly authorized **protective boundary**. `APPROVED_EVALUATORS` is empty.
**All new production freezes remain blocked, including apparently empty Epochs.** There is no flag,
Admin action, public API, command, or count-based fallback to enable them. Phase 3 must supply a
separately approved policy and complete source-verification implementation. Merely inserting a
COMPLETE evaluation row cannot enable the application path. Do not populate this registry to bypass
an operational blocker.

A genuinely empty result is representable by a complete, versioned evaluation whose independently
verified scope and evaluated counts are both zero and whose source closure is proven. Positive-path
tests use an explicitly synthetic policy/ingress seal. They prove receipt consumption, atomicity and
barrier mechanics; they do **not** prove production eligibility or Telegram delivery completeness.

## Economic path investigated

All attribution remains half-open `[starts_at, ends_at)`; exact cutoff belongs to the next Epoch.
An Epoch's immutable config is pinned at initialization to the Season baseline. Individual source
facts/ledgers retain their own effective/pinned published config; none is recalculated by this PR.

| Stage/source | Durable owner and producer | Time/config, pending work and retry contract |
|---|---|---|
| Authenticated identity / Telegram | `Player`; signed Mini App auth and normalized Telegram ingress | Telegram user ID is identity, not qualification. `TelegramInboundUpdate` is durable before dispatch; broker depth is not completeness. `received_at` is not message source time. Late received updates and unresolved dead letters have unknown economic impact. |
| Canonical rewards, Daily, Missions | `RewardLedger`, `PlayerSeason`, progression; reward primitive, Daily claim, Mission claim | Immutable source key/config and `occurred_at`; one canonical XP event projects to Season/Lifetime once. REWARD_READY Mission entitlements retain `verified_at` and can be claimed later; they now block until resolved. No reward is deleted or automatically claimed here. |
| Base Tap v2 | `RewardLedger`, `PerformanceSourceFact`, burst state/leases; `submit_batch` | Receipt/ledger/projections/fact commit atomically at accepted source time. Season shared barrier → Epoch FOR SHARE → Player → burst. Immutable fact owns weekly performance; BASE_TAP ledger is excluded from snapshot reward aggregation. Existing idempotent receipts replay without minting. Valid leases end at the weekly boundary; unsent touches are not committed evidence. |
| Legacy Tap | accumulators, historical receipts and permanent facts; accept/finalize services | Pending XP is retained and blocks, including unknown first-source timestamp. Receipt expiry does not erase permanent facts. Finalization must still settle accepted XP once; this PR does not run legacy drain. |
| Typing | attempts, wallet CREDIT transactions, permanent source facts; submit/claim | Completion time owns CREDIT performance; a later TYPING_CLAIM is excluded from weekly reward aggregation. CREDIT referral performance is also source-time, and its later referral ledger is excluded. STARTED attempts before cutoff remain blockers; unclaimed final wallet credit is not itself a blocker. Claims/auto-settlement keep original Season. |
| Network / Creator | `NetworkActivity`, `CreatorWindowSettlement`, reward ledger; ingestion/window worker | Source-time published config; window reward uses end minus one microsecond, clamped at Season economic end. Unprocessed activity/unfinalized windows can still change inputs. Qualification horizons and window advisory locks preserve existing source-order behavior. |
| Clan / Treasury / HQ / mission work | `ClanActivity`, windows, economic settlements, Treasury/Contribution/HQ ledgers, `ClanMissionSignal`; existing Clan workers | Shared Season barrier, semantic/window locks and canonical economic settlement. Contribution remains attached to historical membership. Unfinished windows (including unknown ends), earlier source work and B6 held/review mission signals block. No Treasury carry/history change. |
| Referral outbox | `RewardSettlementOutbox`, `ReferralShareOutcome`, referral ledger/facts | Source config/time and deterministic source identity. PENDING/PROCESSING/DEFERRED are not completeness. DEFERRED may require explicit existing Season-close handling; it is retained, not silently discarded. Exact source-level outcome coverage belongs in the future verifier. |
| Qualification / moderation | append-only `QualificationDecision`; audited moderation actions plus legacy fallback | Latest exact policy/version decision at cutoff; PENDING/revocation and bans/suspensions retain existing behavior. No producer or thresholds invented. Canonical decision writes now take relevant Season shared barriers before insertion, reject new retrospective changes to frozen cutoffs, and preserve exact replay. Moderation apply/expiry joins active Season barriers before case/Player locks. |
| Drain / evaluation | PostgreSQL providers and `EpochReadinessEvaluation` | Drain is necessary, not sufficient. Global unresolved Telegram ingress is conservative because source scope cannot be proved from received time. Empty queues do not prove no future source arrival. Evaluation must certify its complete population and ingress closure independently. |
| Freeze | `EconomicEpoch`, category budgets, immutable snapshots | Final exclusive Season barrier → Epoch row → revalidate latest receipt/config/cutoff/verifier version/digest and providers → materialize all outputs and FROZEN atomically. A preflight result is never authorization. |
| Future settlement | batches, category carry and DLX allocation/adjustment ledgers | Unchanged chronology, settlement gate, same-category carry and immutable accounting. Scheduled `settle_epochs` still returns zero; no production strategy is installed. |

Existing normalized ingress can arrive after cutoff; `queue_mission_signal` can also create work
without a complete ingress seal. Some downstream services obtain their Season barrier after outer
workflow locks. An approved verifier must reconcile those producer boundaries and prove a stable
source seal before activation. **This PR does not claim that all future source producers are fenced
by an empty provider query.** Keeping the production verifier unconfigured is the protective response
to that unresolved dependency, rather than discarding late work or choosing a lateness policy.

## Evaluation, atomicity and recovery

One additive model is needed: existing QualificationDecision records describe individual outcomes,
not Epoch-level population/completion; existing freeze_watermark is mutable operational state and
cannot independently audit an interrupted evaluation. Attempts pin Epoch/Season (through its protected
FK), policy key/version, config identity, cutoff, evaluator version, stable evaluation identity,
input digest, coverage/source provenance, blocker codes and completion timestamp.

- Missing attempt → `QUALIFICATION_NOT_EVALUATED`.
- Persisted RUNNING attempt → `QUALIFICATION_RUNNING`; a crash cannot look complete.
- Incomplete/rejected attempt → BLOCKED with exact reasons, including `QUALIFICATION_INCOMPLETE`.
- Exception → failed transaction rolls back first; a separate transaction persists FAILED, without
  exception payload/private data. Real process death leaves RUNNING, which can resume with the same key.
- Terminal attempts are immutable and retries return the same result. Changed evidence or a failed
  attempt requires a new evaluation identity; a newer incomplete attempt blocks an older success.
- COMPLETE requires a configured server verifier, full coverage, zero unresolved evaluations,
  source-completion evidence and provenance. It creates no qualification decisions.
- Freeze re-inspects under the exclusive barrier and requires identical evidence digest and exact
  policy/config/cutoff/evaluator version. Evidence changes require reevaluation.
- Expected blockers commit only a recoverable DRAINING marker/watermark, then raise EpochBlocked.
  The task catches this and logs safe reason codes at INFO. No automatic retry storm or partial
  final outputs. Actual database failures roll back outputs; the next scheduled task can retry.
- Concurrent freezes recheck state after acquiring the barrier/row lock. Successful replay returns
  immutable history without re-materializing budgets/snapshots.

The evaluator interface performs bounded database inspection, not remote I/O under the barrier.
Any future activation requires review of its scope, missing/PENDING decisions, config cutovers,
source closure/fencing and all participating writer lock orders. No existing config is mutated here.

## Read-only production preflight

After an approved release installs this command/schema, the operator runs:

```bash
cd /opt/dalexio/app
docker compose exec -T api python manage.py epoch_freeze_preflight S1 1
```

Stdout is JSON. Exit 2 means held/not evaluated; exit 3 means unknown Epoch. Unexpected database/query
failure exits nonzero and must be treated as UNKNOWN, never READY. Redirect stdout separately from
stderr if archiving the report. Use the actual Season code if the fresh inspection differs from S1.

The command starts a PostgreSQL READ ONLY / REPEATABLE READ transaction and a per-statement 10-second
timeout. Queries aggregate in PostgreSQL instead of loading all players. It reports dates/state,
pinned config/checksum, exact policy, latest decisions at cutoff, moderation exclusions, unresolved
coverage (null when unknown), distinct players with ledger/fact activity, source/category counts,
drain blockers, latest evaluation and required action. It exposes no Telegram IDs, message bodies,
credentials or player lists. It does not evaluate, write decisions, freeze, settle or dispatch work.

NOT_EVALUATED and BLOCKED are both unsafe. This release has no production READY path. A future
complete receipt still requires transactional revalidation; a read-only preview does not certify it.
Already finalized Epochs require historical review; the command does not pronounce them legitimate
or reopen/rewrite them. Preserve operator evidence and escalate discrepancies.

For a fresh **pre-release** inspection using the existing production image, this is read-only:

```bash
cd /opt/dalexio/app
docker compose exec -T api python manage.py shell <<'PY'
from django.db import connection, transaction
from apps.seasons.models import Season
from apps.arena.models import QualificationDecision, DlxAllocation, GrowthUnlockEvent
with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '10s'")
    s = Season.objects.get(code="S1")
    print({"season": s.code, "starts_at": s.starts_at, "ends_at": s.ends_at,
           "settlement_gate": s.settlement_start_epoch,
           "settlement_started_at": s.settlement_started_at})
    print(list(s.economic_epochs.order_by("epoch_number").values(
        "epoch_number", "state", "starts_at", "ends_at", "config_version_id",
        "base_budget", "growth_budget", "frozen_at", "snapshots_completed_at")))
    print({"qualification_decisions": QualificationDecision.objects.count(),
           "allocations": DlxAllocation.objects.filter(season=s).count(),
           "growth_unlocks": GrowthUnlockEvent.objects.filter(season=s).count()})
PY
```

Those counts are an observation, not evaluation completeness. No production command was executed.

## Migration, worker cutover and rollback (recommendations only)

1. Owner approves reviewed commit, migration SQL, staging proof and protective hold semantics.
   Confirm all new freezes being held is intentional until the separate policy/source release.
2. Back up normally; restore and rehearse on an isolated copy. Record fresh preflight, deployment
   SHA, all six states, source/ledger counts and existing frozen history. Never reset Epochs.
3. Apply additive `arena.0011` and `0012` with short lock/statement timeouts and an operator-approved
   retry window. 0011 creates an empty table/index/FKs; no history scan/backfill/rewrite. 0012 creates
   functions/triggers; trigger installation briefly requires table locks on Epoch/evaluation tables.
   Long existing transactions can delay DDL; measure locks against actual production workload.
4. The database guard rejects old-worker transitions without a matching COMPLETE receipt and service
   capability. Deploy/reload every worker to the new code promptly: old freeze tasks will otherwise
   report database failures rather than the new quiet readiness blockers. The database guard protects
   the cutover once its migration commits; no pre-migration protection is claimed.
5. Keep Beat schedules and all unrelated workers/reward processing enabled. After cutover invoke
   only read-only preflight, inspect `epoch_freeze_blocked`, verify DRAINING/recoverable state and
   absence of new final outputs/allocations. Verify Tap, Typing, Daily, Mission and Clan earning in
   staging and normal production telemetry. Do not manually create a COMPLETE receipt.
6. No historical eligibility or economic data migration. Existing frozen epochs are not retrospectively
   validated by these migrations; review any discovered finalized Epoch separately without rewriting.

Rollback should retain the additive schema and database guard. Reverting binaries alone leaves old
freeze workers blocked by the trigger (with errors); remove/revert only an operator-approved narrowly
scoped freeze-task patch, while retaining reward writers compatible with the existing B2/B3/B6 schema.
Reversing 0012 removes the protection, and reversing 0011 destroys audit attempts: neither is a safe
production rollback. No integrity trigger is disabled as a recovery technique.

Epoch 1 deadline supplied by owner: **2026-10-21 22:33:06.352397 UTC**
(**2026-10-22 02:03:06.352397 Asia/Tehran**). Review/rollout completion cannot be assumed.
If the approved protective release cannot be installed in time, escalate immediately. The precise
unsafe operation is Beat entry `drain-freeze-dlx-epochs`, task `apps.arena.tasks.drain_and_freeze_epochs`.
A possible emergency operator-approved mitigation is a reviewed temporary worker build that makes
**only that task** return zero, followed by replacement of every old worker before cutoff. Leave
all other task bodies, Beat entries, earning schedules and queues untouched. Account for all in-flight
old freeze tasks during the normal graceful worker cutover; never purge the shared reward queue.
Inspect all six states and retained source/ledger counts before/after. Revert only that task hold after
the guarded release is approved and installed. This is a proposed scoped code hold, not a live command
or permission to deploy. No hold, restart, scheduling change or other production action was performed.

## Remaining decisions and limits

P0: Until approved deployment/migration, the live baseline is not protected by this branch. A fresh
operator report must establish whether any Epoch has already finalized. The reported OPEN states
are historical observations only.

P0 for eventually resuming Freeze: owner must approve the Qualified Player v1 criteria, evaluation
population (including inactive/non-earning accounts where required), revocation/PENDING handling,
effective-time authority and whether/how retrospective evaluations may use historical facts. The
engineering implementation must then demonstrate an authoritative ingestion/source seal, all
per-source config/outcome coverage, delayed delivery handling and writer fencing/lock-order audit.
No positive-XP/authentication shortcut is authorized. No activation deadline is invented here.

P1: Conservative global ingress and retained DEFERRED/review work can intentionally hold an Epoch
until explicit supported disposition. Exact preflight aggregates can time out on large installations;
a timeout is UNKNOWN and requires measured indexing/inspection, never optimistic readiness.

P2: Existing PH-037 branch protection and PH-043 Season-brief decisions remain separate owner gates.
This task neither resolves them nor authorizes Phase 2/3.
