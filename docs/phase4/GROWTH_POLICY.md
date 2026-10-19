# Approved addendum rule: Season-to-date pre-Freeze funding

The owner's Phase 4 addendum replaces the earlier unstarted-only proposal. The supported rule is
`SEASON_TO_DATE_PRE_FREEZE_EQUAL_V1`. There are no Demo/Official branches. Each activated Season
pins exactly one independently approved policy. No configuration/policy/Season is activated in this PR.
Shipped normal examples have Growth disabled; a separate clearly labeled Demo-growth example and
disposable tests exercise the owner's specified 3M/5M/20-player/2M profile.

## Recognition and count

The qualifying cutoff is an economic Epoch's end, never the time a delayed worker happens to run.
The cohort must be authoritative: a complete, fresh readiness evaluation of the prepared cohort,
or the immutable readiness/snapshot proof of an already finalized Epoch. Global source-integrity
blockers prevent Growth. Individual PENDING/DISQUALIFIED cases are not counted. Future source
activity, later Claim timestamps and raw registrations do not qualify.

The canonical counter is distinct Season-to-date participants with at least one policy-matching
QUALIFIED disposition supported by a frozen/finalized Epoch snapshot or the currently prepared
Epoch. Count once across categories, Epochs, repeated decisions and retries. The latest authoritative
Season disposition as of the evaluated cutoff excludes PENDING/DISQUALIFIED players. A player who
was legitimately qualified in an earlier Epoch but is merely inactive later remains a Season
participant; NOT_YET_QUALIFIED for insufficient new weekly activity is not revocation of past valid
participation. Audited moderation/disqualification at the later cutoff removes them from subsequent
counts. It never changes the recorded count, amount or evidence of an already committed unlock.

The same SQL counter backs execution and authorized inspection. Its chosen decision IDs are hashed
in stable player order. The event preserves cutoff, Epoch, population rule, count, digest, policy and
config checksums, readiness ID, evaluation time and exact receiving placements. Separate append-only
GrowthEvaluation rows distinguish threshold-not-reached, funded, replay and unfunded evaluations.

## Finalization and recipient rule

Canonical source finalization → Prepare/Qualification → readiness/source completeness → exclusive
Season barrier → validate authoritative cohort → evaluate unmet milestones → atomically authorize
and place funding → recheck readiness → snapshot budgets/performance → Freeze → Settlement →
Reconciliation. Growth and Freeze share the same exclusive transaction; no source writer or other
Growth worker can invalidate the cohort or commitments between authorization and Freeze.

Eligible recipients are the cutoff's own DRAINING Epoch and later OPEN/DRAINING Epochs whose
periods begin at or after that cutoff. Each must have no category budget or performance snapshot.
A later currently running OPEN Epoch can receive a prior finalized cohort's funding. Merely being
stale OPEN is insufficient: an earlier period predating the recognition cutoff is excluded.
FROZEN/SETTLING/SETTLED Epochs never receive funding or reopen. Delayed scheduling does not erase
the current Epoch's eligibility while it remains safely pre-Freeze. No closing Epoch is created.

Process reached milestones in threshold/key order. Divide each integer amount equally across
recipient Epochs ordered by number; assign remainder units to the earliest recipients. Unique
Season/milestone and event/Epoch identities, the Season barrier and row locks serialize retries.
A positive event, every immutable placement and all Epoch budget projections commit together.
A deferred PostgreSQL conservation trigger also rejects positive authorization without exact funding.

If no eligible receiver remains, record the reached milestone once with zero unlocked amount,
requested amount and `NO_ELIGIBLE_MUTABLE_EPOCH`. It is eligible-but-unfunded, does not increase
authorization and is separate from a funded milestone in Admin reports. Subsequent evaluations
record replay without inventing funding. No milestones configured means zero Growth and no evaluation.

## Required two-Epoch examples

Configuration: base 3,000,000; maximum 5,000,000; 20 distinct qualified participants unlock 2,000,000;
two seven-day Epochs, one closing day and settlement gate 1.

| Scenario | Count/proof | Epoch 1 Growth | Epoch 2 Growth | Authorized total |
| --- | --- | ---: | ---: | ---: |
| A | first cutoff establishes 20; Epoch 1 DRAINING, both mutable | 1,000,000 | 1,000,000 | 5,000,000 |
| B | first cutoff 10; final cutoff adds 10 new participants, earlier 10 remain eligible | 0 | 2,000,000 | 5,000,000 |
| C | 20 proven but all receivers already immutable | 0 | 0 | 3,000,000; durable non-funding decision |
| D | at most 19 distinct participants | 0 | 0 | 3,000,000 |
| E | A/B evaluated repeatedly or concurrently | unchanged | unchanged | at most 5,000,000 |

10,001 units across three eligible Epochs is exactly 3,334 / 3,334 / 3,333. The engine uses identical
logic for the six-Epoch official profile and retains its configured settlement gate and all immutable
history. With no configured milestones, official budgets and Phase 3 economics do not change.

## Conservation and locking proof

For amount A and r>0 recipients, q=A div r and m=A mod r. Exactly m portions are q+1 and the rest
q, totaling rq+m=A. If r=0, authorized amount is zero. Cumulative authorization is checked against
max−base under the Season lock. Every positive unit has a normalized receiving record; the deferred
constraint enforces event sum = placement sum at commit.

Base and Growth category splits each conserve integer totals. Fresh category funding = base+Growth;
participation, individual caps, correction reserves and corrected-cohort calculations all use that
same sum. Fresh+carry_in = normal allocation+reserve+carry_out. Same-category Carry telescopes across
the configured Epoch sequence. Corrections consume only funded original reserves. Thus
base+authorized Growth = normal allocations+funded corrections+final carry+unused reserves.
Interrupted funding rolls back the event, placements, budget projections and Freeze together.
Confirmed allocations and prior commitments are never modified. No XP-to-DLX conversion is introduced.
