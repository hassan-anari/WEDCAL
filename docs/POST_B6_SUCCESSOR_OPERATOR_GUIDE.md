# Draft #33 successor: live Clan ranking cleanup and one-time QA recreation

Owner-approved continuation of the post-B6 hotfix. The successor SHA in
[Draft PR #33](https://github.com/hassan-anari/WEDCAL/pull/33) and
[FROCAL #8](https://github.com/hassan-anari/FROCAL/issues/8) supersedes
`07876b3ebfa7ef4ede8d354682ae49add7a23150` for paired validation.
No merge, deployment, production rebuild or QA-account grant was performed by
this development task. The commands below are the exact activation procedure
for the deployment operator after separate owner approval.

## 1. Live Clan ranking contract

An `ENDED` ClanSeason is ineligible for **live** Clan rankings regardless of its
retained score. `RECOVERY` retains its previous score-based eligibility. Both
singleton immediate dissolution and approved Captain/recovery governance
dissolution increment `ranking_generation` and persist a `REMOVE` projection
event in the same PostgreSQL transaction as membership release/state change.
A rejected vote creates no removal. Scores, Treasury, ledgers and source history
are not reset, rewritten or replayed.

Event delivery checks the durable ClanSeason lifecycle. Even an older queued
UPSERT is applied as REMOVE for an ended Clan, at least at its current generation.
Rebuilds emit no positive sorted-set member for ended Clans and retain generation
tombstones; Redis loss or out-of-order event replay cannot restore their live
membership. Rebuild watermark/tail replay and existing generation fences remain.
Finalization barriers are acquired before ordinary dissolution row locks, so
ranking writes cannot cross an established Season finalization barrier.

Live API reads preserve the existing response shape. Pending projection events,
canonical/projection population mismatch or page-row lifecycle/generation drift
fail closed with the existing retryable 503 reconciliation/unavailable contract
and schedule repair; the API never filters a Redis page locally and invents
replacement ranks/counts. After projection reconciliation, ranks are compact,
`total_count` and `next_offset` use the eligible population, and a released former
member has no `own_standing`. Canonical Clan summaries use the same eligibility.
Closed/archived API pages and canonical Clan summaries use immutable final
standings. Historical Redis rebuilds use those final snapshots, including Clans
that later have an ended lifecycle; no finalized rank/score is changed.

## 2. Reconcile Clans dissolved before this fix

This is required after deployment: earlier dissolutions did not publish REMOVE
events. Do not zero their scores, delete Clan rows, delete memberships, issue
compensating economics, edit final standings or run Redis KEYS/FLUSHDB.

1. Deploy the approved successor application and apply its additive migrations
   using the usual release procedure. Replace old API and worker processes so
   no old ranking worker can apply an unguarded UPSERT. Keep normal event workers
   and the existing periodic reconciliation enabled. Do not reverse B6 carry or
   mission lifecycle migrations.
2. Select the exact active Season code from authorized Django Admin. Choose an
   active Django operator with `arena.change_rankingprojectionstate` (or an
   existing authorized superuser). This permission controls the repair command;
   it is not a player permission or a Mini App endpoint.
3. From the deployed backend application environment, run:

```bash
uv run python manage.py rebuild_live_clan_rankings \
  --season '<CURRENT_SEASON_CODE>' \
  --actor '<AUTHORIZED_DJANGO_OPERATOR_USERNAME>' \
  --reason 'Post-B6 owner-approved reconciliation of dissolved Clan live ranks'
```

The command rebuilds only that active Season's CLAN projection using the existing
leased temporary-key/swap/tail-replay machinery, then processes its pending Clan
events through the lifecycle guard. It writes `REBUILD_LIVE_CLAN_RANKINGS` to
`OperatorAuditEvent` and outputs `rebuilt=True eligible_population=<count>`.
No player ranking, financial projection or historical snapshot is rewritten.
If a rebuild lock is unavailable/expired, retry after the existing worker/lease
finishes; do not delete coordination keys. If the command fails mid-rebuild,
retry it; PostgreSQL remains truth and the derived Redis projection is repairable.
CLOSED/ARCHIVED or expired Seasons are rejected by this operational command.

4. Authenticated QA: refresh Arena Clan ranking page one and subsequent pages.
   The dissolved Clan must be absent, ranks contiguous, count/pagination coherent,
   and surviving Clan own standing correct. Refresh after concurrent ranking
   events; temporary 503 means reconcile/retry, never show guessed ranks.
   Verify the old Clan's retained score/Treasury/history through authorized
   read-only inspection. This verifies cleanup, not an economic replay.

The existing `apps.arena.tasks.reconcile_rankings` periodic task also uses the
corrected rebuild path. The explicit command provides deterministic operator
execution and an audit trail instead of relying on its next scheduled run.

## 3. Grant the owner's QA Captain exactly one recreation permission

General same-Season recreation remains prohibited. There is no general unlock,
GameConfig edit, account reset, reactivation or replacement of the ended Clan.
This owner-approved administrative exception is represented by one retained
`ClanRecreationGrant` for one player and one Season. Its reason, owner approval
reference, actor and timestamps are durable. Unique player/Season identity and
PostgreSQL history guards prevent deletion/rebinding/reset/regrant after use.

Prerequisites:

- The **owner-designated account** must already exist as a Player and have the
  Captain role and an ENDED ClanSeason in the current active Season.
- It must have no current membership or live Clan. A different player's grant
  cannot authorize it, and a grant in a different Season cannot authorize it.
- Use an active Django operator granted the new dedicated permission
  `clans.grant_qa_clan_recreation` (or an existing authorized superuser). Ordinary
  staff status alone does not authorize this operation. The permission is created
  by migration/post-migrate; assign it through the existing owner-controlled
  Django User/Group administration. No permission is automatically assigned to
  players or ordinary operators.
- Obtain the specific Telegram numeric account ID **privately from the owner**.
  It must match the authenticated QA Captain; never use a public username as
  identity or paste the ID into source, fixtures, PRs or public documentation.
  No real account ID is embedded in this feature.

Run in Bash from the deployed backend application environment, replacing only
the three non-secret placeholders. Enter the owner-specified account ID at the
hidden prompt; it is passed on standard input, not a command argument:

```bash
read -r -s -p 'Owner-designated QA Telegram numeric ID: ' qa_telegram_account_id
printf '\n'
printf '%s\n' "$qa_telegram_account_id" | uv run python manage.py grant_qa_clan_recreation \
  --telegram-user-id-stdin \
  --season '<CURRENT_SEASON_CODE>' \
  --actor '<AUTHORIZED_DJANGO_OPERATOR_USERNAME>' \
  --reason 'One-time owner-approved QA Captain recreation after dissolution' \
  --ticket-reference '<PRIVATE_OWNER_APPROVAL_REFERENCE>'
unset qa_telegram_account_id
```

An application container without `uv` should run the same command with its
installed application Python: `python manage.py ...`; flags and stdin are
identical. Do not enable shell tracing (`set -x`) while entering the account ID.

Expected first success: `grant=<id> created=True consumed=False`.
A retry with the **same actor, player, Season, reason and approval reference**
returns the same grant with `created=False`, including after consumption. It
never replenishes the authorization. Changed grant details for the same
player/Season are rejected. Preserve the output grant ID privately with the
operator's approval record. Audit action: `QA_CLAN_RECREATION_GRANTED`.

### How the owner uses it

1. Reopen/refresh authenticated Mini App Clan state for the designated account.
   `GET /api/v1/clans/state` adds
   `qa_recreation_authorization_available: true` only for its unconsumed grant.
   It is false for normal accounts and defaults false on older consumers.
2. Use a **new eligible Telegram group**: owner/Captain admin authority, bot
   admin/invite capability, known candidate/membership, progression/role and
   exclusive economic context are still verified by the normal registration
   service. Use a distinct available Clan tag/name. No client grant token or
   arbitrary account ID is accepted by the creation API.
3. Submit the ordinary `POST /api/v1/clans/` registration. The server locks the
   Player and authorization, rechecks all restrictions, creates a new Clan and
   ClanSeason and consumes the grant in that **same transaction**. Audit action:
   `QA_CLAN_RECREATION_CONSUMED`, linked to the new ClanSeason and original grant.
   Failed validation/authorization/creation rolls back consumption. An identical
   successful registration replay returns the existing live Clan and consumes
   nothing further. Concurrent different-group creation cannot spend it twice.
4. Confirm the new membership is active and state now reports
   `qa_recreation_authorization_available: false`. The old ended ClanSeason,
   released memberships, events, score, Treasury and economic records remain.
   If the new Clan is later dissolved, no second recreation is permitted by the
   consumed authorization. Do not delete/reissue the grant to reset that limit.

### The old dissolved Clan's Telegram group cannot be reused

`Clan.telegram_group` remains a persistent one-to-one ownership relation. The old
Clan identity still owns its old Telegram group after dissolution. The grant does
**not** detach that group, delete the Clan or bypass this constraint. Bound groups
are omitted from eligible-group reads, and forged registration with a bound group
is rejected without consuming the grant. Use another eligible group. Any product
change to historical group reuse requires a separate approved design.

## 4. Compatibility, migrations and rollback

New migrations: `clans.0020_qa_recreation_grant` (additive table, permission,
uniqueness/consumption constraints) and `clans.0021_qa_grant_history_guard`
(immutable identity/history and player/Season-scoped consumption guard). There
are no data migrations or granted exceptions by default. Apply before new
application processes serve state/registration. Inspect SQL using:

```bash
uv run python manage.py sqlmigrate clans 0020
uv run python manage.py sqlmigrate clans 0021
uv run python manage.py makemigrations --check --dry-run
```

Do not drop grant/audit history or reverse migrations once used. A forward fix
can stop further grant issuance; existing registration remains subject to its
recorded authorization. Old code cannot safely serve multiple same-Season
historical ClanSeasons for the QA Captain; keep the successor Captain lookup and
live ranking guards during rollback. No automatic account recreation or reset.

The original Network category filter, notification defaults/legacy presentation,
governance error codes, IMMEDIATE/VOTE_REQUIRED/BLOCKED readiness, voter minimum,
quorum, approval and economic settlement remain. The existing dissolution warning
and `same_season_recreation_allowed=false` describe the general policy; the
separate QA authorization is an explicit, audited exception only after an ended
Clan and operator grant, not a relaxation of those safeguards.
