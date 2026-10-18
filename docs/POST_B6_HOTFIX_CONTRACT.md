# Post-B6 production QA hotfix contract

Scope: reward history reads, Clan In-App presentation, governance diagnostics and
preflight. Base: WEDCAL main `76a3bec44b68e4b8d5928ddf10999d8d82a2f6f0`.
This document and [current OpenAPI](fixtures/b6/openapi.json) supersede only these
parts of the B6 contract. Other B6 contracts and safety rules remain in force.
The exact consumer checkout SHA is posted on the Draft PR and FROCAL issue #8.

## Authentication and compatibility

All routes below use the existing `Authorization: tma <signed Telegram initData>`
player authentication. Reads are private/no-store and vary by Authorization.
No request accepts an account ID as proof of identity. No migrations, new ledger,
settlement records, economic configuration changes or historical rewrites.

## Network reward history

`GET /api/v1/rewards/history?category=network&limit=20&cursor=<integer>`

- `category` is optional. Its only accepted supplied value is exactly `network`.
  Omission retains **all** sources for Base Recent Rewards and existing consumers.
  Empty, `all`, `NETWORK`, `creator` and other unsupported values return 422
  `REQUEST_INVALID` using the existing typed error envelope.
- `network` matches exactly `CREATOR` and `NETWORK_SPEAKER`, independently of
  context, across the player's existing history. It does not include Referral,
  Clan or Tap sources. No new earnings/claim/settlement mechanism exists.
- Descending ledger `id` order is unchanged; this is receipt order, not an
  `occurred_at` sort. The source filter and `id < cursor` predicate run in SQL
  before `LIMIT limit+1`. The lookahead uses that same filtered query.
- Default limit 20; existing clamping to 1..100 remains. Cursor is the existing
  exclusive integer ledger ID. Arbitrary or foreign IDs are only boundaries;
  the authenticated player predicate always applies. Noninteger cursors return
  typed 422 `REQUEST_INVALID`.
- Success remains `{items: HistoryItem[], next_cursor: integer|null}`.
  Each item has `id`, `source_type`, `context`, `player_amount`, `occurred_at`.
  `next_cursor` is the last returned matching ID only when another matching row
  exists. Empty and final pages have null; exactly `limit` matches can be final.
  Empty matching history is `{ "items": [], "next_cursor": null }`.

FROCAL: use `category=network` for Creator/Speaker history on **every** page;
remove local filtering as the pagination authority. Keep Base requests unfiltered.
Key the cache by authenticated account and category; restart pagination when
changing either. Pass `next_cursor` unchanged and stop on null. Concurrent new
rewards appear on page-one refresh; older-page requests cannot duplicate them.
Do not infer an empty history from locally filtering an unfiltered first page.

Example (illustrative IDs):

```json
{
  "items": [
    {"id": 81, "source_type": "CREATOR", "context": "NETWORK_GROUP",
     "player_amount": 12, "occurred_at": "2026-10-18T00:00:00Z"}
  ],
  "next_cursor": 81
}
```

## Clan In-App notifications

Existing list and single-read routes retain their schemas, event types, public IDs,
payloads, source attribution, read/unread state and cursor behavior.

- Enabled In-App templates still take precedence; highest enabled version wins.
  Explicit announcement snapshots retain their existing behavior. Nonblank saved
  title/body fields are returned unchanged.
- Missing templates get static English default copy for the important existing
  Clan events. List and read responses fill blank/whitespace legacy fields from
  the same defaults **without writing notification history or looking up today's
  templates**. A saved template's nonblank text is never replaced.
- Defaults never interpolate payloads, names, Telegram links, credentials or tokens.
  `CLAN_INVITE_READY` title is **Clan invitation ready**; body is:
  **Open Clan Status in the app to securely access your personal invitation.
  Joining Telegram still requires membership verification.**
- The safe existing `deep_link: {path: "/clan"}` routes to Clan. Fetch access only
  through authenticated Clan Status / `/clans/invite/current`. An accepted request
  or ready invitation is not an ACTIVE membership.
- Join acceptance/decline, verified membership activation, access restoration and
  recovery/restoration transitions now emit In-App notifications from their
  existing committed source records. They use deterministic new lifecycle keys;
  replaying a transition does not emit a second event. No historical replay/backfill.
- Existing invite-ready/access-lost/governance/warning/authority-loss keys, source
  identity and payload semantics remain unchanged. Defaults/templates are not
  treated as explicit caller text when validating a concurrent duplicate. A legacy
  blank event replay remains the same stored event and does not adopt new text.
- New lifecycle notifications are In-App only. Existing selected Telegram delivery
  rules remain unchanged. No invite credential is sent privately; B6's fence also
  continues to block old queued `CLAN_INVITE` Telegram deliveries.

## Governance conflicts

The existing `{code, detail, retryable, retry_after_seconds}` envelope remains.
Business conflicts below return HTTP 409, `retryable=false`, null retry-after.
`detail` is the sanitized code converted to lowercase words; clients branch on
`code`, not prose. Authorization and missing own resources keep existing 403/404
behavior (`CLAN_FORBIDDEN`, `CLAN_NOT_FOUND`). Shape/choice parsing stays 422.

| Code | Meaning / UI action |
|---|---|
| `CLAN_GOVERNANCE_INSUFFICIENT_VOTERS` | Eligible electorate is below the pinned minimum. Refresh readiness; do not retry in a loop. |
| `CLAN_GOVERNANCE_OPEN_CASE_EXISTS` | A different request of the same governance type is already open. Show existing case. |
| `CLAN_GOVERNANCE_INVALID_TARGET` | Target is absent, ended, Captain or outside this Clan. Refresh own roster. |
| `CLAN_GOVERNANCE_INVALID_REMOVAL_REASON` | Use a supported removal reason. |
| `CLAN_GOVERNANCE_RECOVERY_RESTRICTED` | Ordinary Captain dissolution/removal is unavailable during recovery. Show recovery state/reasons. |
| `CLAN_GOVERNANCE_CLAN_ENDED` | ClanSeason already ended, including an immediate dissolution retry. Refresh canonical state. |
| `CLAN_GOVERNANCE_SEASON_UNAVAILABLE` | Clan's Season is outside its active interval/status. |
| `CLAN_GOVERNANCE_STATE_CONFLICT` | The requested governance type is incompatible with the Clan's current state. |
| `CLAN_GOVERNANCE_POLICY_UNAVAILABLE` | Pinned configuration has no Clan policy; operator review required. |
| `CLAN_GOVERNANCE_VOTING_CLOSED` | Case is closed or deadline reached. |
| `CLAN_GOVERNANCE_VOTE_ALREADY_CAST` | The voter already submitted a ballot. |
| `CLAN_GOVERNANCE_INVALID_VOTE` | Invalid domain vote choice; HTTP schema normally rejects this at 422. |
| `CLAN_GOVERNANCE_INVALID_CASE_TYPE` | Unsupported internal case type; not client selectable. |
| `CLAN_GOVERNANCE_CASE_NOT_DUE` | Finalization before the deadline; worker/domain use only. |

Pre-router current-Season failure retains existing `SEASON_UNAVAILABLE`.
Mutation transaction locks, captain checks, electorate snapshots, target exclusion,
minimum/quorum/approval, Season and recovery checks are preserved. One open case
per ClanSeason **per case type** is the existing rule: a removal case and a
Captain dissolution case may coexist. No cross-type exclusion is introduced.
An identical open request still returns its existing case; a different same-type
request conflicts. Targets cannot vote on their own removal. Suspended and banned
players remain excluded; access-loss membership alone does not remove a voter.

## Server-authoritative dissolution confirmation

Authenticated own-member `GET /api/v1/clans/recovery` adds optional `governance`.
Old clients can ignore it. New clients must tolerate absence on an older backend,
refresh before confirmation, and never infer readiness from local roster counts.
Only this authenticated own-Clan read exposes the information, not public Clan
Discovery. Existing recovery fields remain unchanged.

`governance` contains the actual ClanSeason-pinned published `config_version`,
`config_checksum`, `minimum_eligible_voters`, `quorum_bps`, `approval_bps`,
`vote_duration_hours`, `dissolution`, and `removal_targets`.

Every action contains `can_open`, `blocked_reason` (same API code or null),
`eligible_count`, `existing_case_id` (same-type open case or null).
Dissolution additionally contains:

| Field | Contract |
|---|---|
| `outcome` | `IMMEDIATE`, `VOTE_REQUIRED`, or `BLOCKED` |
| `direct` | Captain-only current roster; outcome remains authoritative if another prerequisite blocks it. |
| `same_season_recreation_allowed` | Always `false`; existing same-Season creation prohibition. |
| `confirmation_warning` | `Dissolution permanently ends this ClanSeason. You cannot create another Clan in the same Season.` |

- **IMMEDIATE:** Caller is Captain, Season and Clan state allow the action, and
  the only current membership is Captain. Confirmation immediately closes the
  ClanSeason and returns **204 with no body**. It does not open a vote or require
  the minimum electorate. The recreation consequence must be shown before confirm.
- **VOTE_REQUIRED:** More members remain and all current prerequisites permit
  opening a case. Confirmation returns **200 GovernanceOut**, opening the timed
  Captain dissolution case. It does not immediately end the Clan. Approved
  finalization ends the ClanSeason; same-Season recreation remains prohibited.
- **BLOCKED:** A current prerequisite prevents a new action. Show `blocked_reason`
  and the pinned minimum/count. If `existing_case_id` exists, offer that case's
  status; identical request replay still returns its existing case.

`removal_targets` lists current own-Clan Soldiers by `target_membership_id` with
**target-specific** electorate counts. It subtracts the target only if that
player otherwise qualifies. A blocked player's exclusion is not counted twice.
All counts use the same domain electorate predicate as mutation. The readiness
read locks only its ClanSeason row (not the shared published configuration) for its snapshot; it is advisory and reserves
nothing. Races can still yield 409 at confirm; display its reason and refresh.

## Production evidence and small-Clan policy decision

Owner-supplied read-only evidence (2026-10-18): published and ClanSeason-pinned
config are both `s1-test-b2-v2`, schema 3, checksum
`148b8fd1602e184d1ae021222ef83fcf3d212f41c7205eb26db9851ce79507e7`.
Minimum voters **3**, quorum **4000 BPS**, approval **6000 BPS**. At inspection,
Dexo Caln / ClanSeason 3 and its Season were ACTIVE, with **one** active member
and one eligible dissolution voter. Owner subsequently dissolved it successfully
as Captain-only; same-Season recreation was blocked, as approved.
This is supplied production evidence, not a live database read performed by this
hotfix agent. No production data, config or rewards were modified here.

The original QA reported **two** active members. With the confirmed pinned
minimum of three, those reported states cannot open either case: dissolution has
at most **2** eligible voters, and removal of the Soldier at most **1** because
the target is excluded. The old domain raises `Insufficient eligible voters` for
both, and the old router collapses both to `CLAN_STATE_CONFLICT`. This explains
the voter precondition independently of the sample config. The later one-member
inspection is a different state; no original timestamped response/trace was
provided to rule out an earlier open-case/recovery/invalid-target failure taking
precedence. Do not label the later successful singleton action as a failure.

Genuine deadlock under minimum 3: a two-member Clan cannot dissolve by vote or
remove its Soldier by vote; a three-member Clan can propose dissolution but cannot
remove an otherwise eligible Soldier (only two voters remain). Moderation may
reduce the electorate further. A small recovery Clan can also lack sufficient
voters for automatic recovery governance. Captain cannot bypass this by Telegram
ban; no succession, unilateral kick or threshold exception is introduced.

Owner decision alternatives, **not implemented**: retain the policy and document
an eligible Soldier's existing voluntary-leave path (with its normal limits/
cooldown), after which a singleton Captain may dissolve; define a separately
approved audited administrative recovery/release process for genuine deadlocks;
or explicitly approve a future effective-dated policy/contract change to the
minimum or a consent-based small-Clan exit. Lowering a future published config
alone does not repin existing ClanSeasons. Do not silently change pinned configs,
remove voting safeguards or permit same-Season recreation to solve this UX issue.

## Paired validation and rollout

FROCAL must explicitly acknowledge this contract and pin the finalized hotfix SHA
before pairing. Verify Network pagination past newer Tap/Daily records; Base
history unchanged; blank legacy notification list/read presentation and secure
Clan route; singleton irreversible confirmation, blocked two-member state, valid
larger-Clan vote, per-target removal counts, recovery/open-case/ended retries and
cross-account cache isolation. Use synthetic isolated PostgreSQL/staging data;
physical Telegram acceptance remains a separate owner/consumer gate.

Draft only, unmerged and undeployed. No feature/config activation, private invite
messages or production mutations. Code rollback preserves all ledger, governance,
notification and membership history; old clients ignore the additive read fields.
Do not relax policy or reconstruct/replay historical records during rollback.

Consumer audit: FROCAL main's `src/integrations/api/rewardHistory.ts` currently
loads unfiltered pages and then filters sources locally. Update that transport
and its cache/controller together. `src/features/clan/ClanStatusSheet.tsx`
currently says “Your Clan remains active until the request is resolved” for all
dissolutions. That is incorrect for singleton immediate dissolution. Replace this
confirmation using the server outcome/warning above, with a fresh preflight; do
not retain that universal vote wording. No frontend files were changed here.

Version-skew gate: an older backend may ignore the unknown `category` query
parameter and return unfiltered history. Do not activate the server-filtered
consumer against that backend. Pair against this exact checkout in isolation and
release the consumer only after the reviewed compatible backend is available.
If governance preflight is absent, keep destructive confirmation unavailable;
do not reconstruct IMMEDIATE/VOTE_REQUIRED from local membership counts.

## Owner-approved successor to 07876b3 — live ranking and QA exception

Draft #33 now also excludes ENDED ClanSeasons from live Arena projection,
publication, rebuild and canonical Clan summaries. Dissolution persists a
new-generation REMOVE without changing earned scores; stale events cannot
reinsert ended Clans. RECOVERY remains eligible; immutable CLOSED/ARCHIVED
standings remain authoritative. Live response shapes are unchanged; existing
503 reconciliation/unavailable responses cover asynchronous projection repair.

Authenticated `GET /api/v1/clans/state` adds
`qa_recreation_authorization_available: boolean = false`. This reports a single
explicit administrative grant for the authenticated player/current Season.
Ordinary registration consumes it atomically only when the existing same-Season
recreation restriction applies and all other eligibility checks pass. No grant
mutation endpoint or client-supplied grant/account selector is added.

The normal recreation prohibition and dissolution outcome/warning fields remain.
The sole exception is the owner-approved, one-use QA grant; it does not allow
reusing the historical Clan's bound Telegram group. New grant/history migrations
must be applied before this successor runs. Exact account activation, permissions,
private stdin command, new-group requirement, ranking cleanup/rebuild and rollback
are in [the successor operator guide](POST_B6_SUCCESSOR_OPERATOR_GUIDE.md).
The new final SHA posted in Draft #33/FROCAL #8 supersedes the earlier pin for
paired tests. No production activation, merge or deployment has been performed.
