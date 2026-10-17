# B6 backend contract

WEDCAL owns this additive contract; FROCAL consumes it. Baseline: merged main `b4c72f292ab0ea967d62af7b8e5fdcab1f27e03f`. [Initial audit](B6_AUDIT.md), [operator guide](B6_OPERATOR_GUIDE.md), [security/economic review](B6_SECURITY_ECONOMIC_REVIEW.md), [OpenAPI](fixtures/b6/openapi.json). Exact published runtime revision is recorded in the final PR/coordination handoff and `B6_RUNTIME_PIN.md` when published. Existing routing, authentication, Treasury history and mutation endpoints are preserved.

## Applicant summaries

GET `/api/v1/clans/applicants` preserves every existing field and adds:

| Field | Type | Meaning |
| --- | --- | --- |
| rank | integer or null | Current Season Player position from Arena's canonical Redis projection |
| rank_status | RANKED / UNRANKED / UNAVAILABLE / RECONCILING | Null rank is explicitly classified |
| current_streak | integer | Actual current Daily Claim streak; zero after missing a complete configured claim period |

Identity is always the authenticated Captain; there is no request-selected Player identity. Existing own-Captain request filtering and bounded limit remain. Empty lists perform no ranking Redis operation. A nonempty page uses one atomic Redis script, not one call per applicant. Pending projection events, score and generation mismatches are fenced before/after the read, using the same Arena readiness/score/generation rules. A failed/rebuilding projection produces null/UNAVAILABLE; pending or stale projections produce null/RECONCILING. No independent score sorting is used. Daily uses the canonical reset timezone/hour/minute and claim records in two bounded queries, shared streak calculation with get_daily_state. It does not use Typing attempt streaks, demo activity or client clocks.

## Clan Season standing

Existing `ClanOut.rank` is populated in Clan detail, creation and discovery responses. Additive `ClanOut.rank_status` has the same four statuses above. GET `/api/v1/clans/home` adds `rank` and `rank_status` for the existing HQ Season Standing presentation. Use one of these server summaries; null is not rank zero.

Clan standings read the exact Arena CLAN projection keyed by ClanSeason identity, validate its competitive score and ranking generation and reject pending/stale projections. Discovery batches the entire bounded page in one atomic Redis read. It does not read the top leaderboard per Clan or maintain an alternative leaderboard. Existing persistent `ClanOut.id` identifiers and Arena's seasonal identifiers are unchanged.

## Group images and exclusivity

Existing `GroupOut.image_url` is now a secure backend group-avatar capability URL when an unexpired cached image exists; otherwise null. Rendering fallback is the existing group initials/placeholder. Group metadata remains server-owned; internal candidate ID is not a Telegram chat ID. Cached bytes use the existing sanitized private storage/proxy, MIME/path/pixel/size controls, refresh slots and Telegram budgets. No token, file ID, bucket key or storage credential is returned. Known bot-admin candidates can be refreshed before Clan registration. Reads schedule cache metadata only and do not call Telegram synchronously.

Eligible list, fresh authority verification (before and after remote calls) and final registration exclude Network registrations in every non-INACTIVE state, preserving the existing stricter final ownership rule. Final registration keeps the TelegramGroup row lock. A Clan add-bot observation does not create a Creator intent or registration; only a valid server-owned Network intent can do that. Clan and Network economic ownership remain exclusive.

## Mission display and administration

Clan mission responses add `title` and `description`; legacy blank titles fall back to the key. Existing fields, rewards, requirement availability and automatically settled participant state remain. No claim endpoint or manual settlement/progress editing is added.

Personal and Clan Admin workflows create disabled drafts, validate, preview actual published policy amounts and explicitly activate/deactivate. Activation requires model change plus dedicated activate permission, a fresh signed actor-bound preview, reason and explicit confirmation. Version identity is immutable; activated or progressed economic semantics require a new version. Supported Clan requirements remain exactly VALID_CLAN_ACTIVITY, CLAN_CONTRIBUTION and VALID_TYPING_RUN. Published rewards are selected by band, never entered as arbitrary amounts.

Clan mission eligibility now uses source time within durable `[activation, deactivation)` intervals as well as `[starts_at, ends_at)`, including delayed signals after deactivation. Reactivation never fills inactive gaps. Approved prospective cutover and uncertain-history holds require `clans.0019_b6_mission_active_intervals`; see the [lifecycle review](B6_MISSION_LIFECYCLE_REVIEW.md). No HTTP/OpenAPI fields change in this follow-up. FROCAL must acknowledge the source-time behavior and re-pin the new WEDCAL runtime.

## Welcome and invites

A distinct private `/start` queues a new Welcome from the currently active Admin template. Idempotency key is `bot-welcome:update:{telegram_update_id}`. Same-update redelivery cannot replace a snapshot or queue another response. Delivery notification payload records template ID/version and update ID; source_received_at preserves inbound receipt time. Existing HTML/keyboard validation, enablement gates, activation/rollback/operator tests, durable dispatch, shared Telegram budgets, bounded retries and ambiguous-outcome handling remain. Activation alone sends nothing.

JOIN and REJOIN invite credentials are delivered only by the existing authenticated owner-only `/invite/current` endpoint. Creation still performs Telegram invite creation and later membership verification; ACCEPTED is not ACTIVE. Automatic private bot delivery is removed, and a dispatch guard marks legacy queued/retry Clan-invite deliveries SKIPPED with `invite_miniapp_only` before any HTTP. Existing delivery_state is retained as compatibility metadata; it is not evidence that an invitation should arrive in Telegram. No public credential endpoint is added. Already in-flight pre-upgrade remote sends cannot be recalled; drain/stop old workers before cutover.

## Recent activity

GET `/api/v1/clans/activity?limit=20&cursor=<opaque>` is authenticated, read-only and restricted to the current own-Clan membership. Existing current-member read permissions also apply during ACCESS_LOST/RECOVERY; outsiders cannot choose a Clan or Player identity. Default 20, valid range 1–50. Malformed, oversized or wrong-Clan signed cursors return 422 ACTIVITY_CURSOR_INVALID. Existing authentication and private/no-store cache semantics apply.

Response: `{items: ClanActivityOut[], next_cursor: string|null, as_of: datetime}`.

| Item field | Type / meaning |
| --- | --- |
| id | Stable namespaced durable record identity; use as list key |
| event_type | CLAN_REWARD_SETTLED, MISSION_COMPLETED, TREASURY_CREDIT, TREASURY_DEBIT, ITEM_PURCHASED, ITEM_ACTIVATED, MEMBER_JOINED |
| occurred_at | Authoritative event/economic time from the canonical record |
| recorded_at | Actual durable recording time where stored; null otherwise, never invented worker time |
| actor_name | Safe first name where relevant, otherwise null |
| amount / unit | Player net points or Treasury credits for this event; null for events without a single amount |
| treasury_amount / contribution_amount | Settlement split and Contribution, where relevant; null otherwise |
| item_id | Canonical purchased/activated item key, otherwise null |
| mission_key / mission_version | Completed definition identity, otherwise null |

Ordering is descending `(occurred_at, id)`; numeric record IDs are fixed-width within their namespaces. Cursor uses that ordering and is signed/scoped to ClanSeason. A page reads at most limit+1 from each of six canonical sources, merges bounded results and returns at most limit. No unbounded in-memory history or offset scan. `as_of` is read time, not a frozen-history snapshot. Late finalized events retain their older occurred_at; refresh page one to discover newly committed history. Clients must not substitute recorded_at/as_of for source time or assume cursor pagination is a settlement barrier.

Reward events represent finalized positive Clan activity settlements, including the Player/Treasury/Contribution split. Mission completion is one event per progress record (Group once per Clan, Individual per member), not one fabricated event per current roster member. Treasury entries already represented by a settlement or purchase are suppressed. Item activation means a successful durable use, not a still-active effect; current effect expiry comes from equipment/server time. MEMBER_JOINED uses verified ACTIVATED membership events, not application acceptance. Zero-Gross windows and unverified/raw Telegram activity are absent. No message text, Telegram identifiers, credential, source payload or private URL is returned. Feed amounts are different accounting units and must not be summed as income. Treasury history remains unchanged and complete for its ledger.

## Owner-approved Treasury carry

B6 prospectively changes fractional splitting, without changing the published split rate. For each ClanSeason/config accumulator: `treasury, remainder = divmod(old_remainder + gross * treasury_split_bps, 10000)`; `player = gross - treasury`. Remainder is durable, integer and constrained to 0..9999. A rollover may produce zero Player net; the canonical ledger records that conserved split while Player points and ranking tie timestamps remain unchanged. Generic zero rewards remain rejected. Same Clan lock/transaction, source-time config, reward/Treasury ledgers, idempotency and projections remain authoritative. Audit metadata identifies B6_FRACTIONAL_V1, denominator, rate and before/after remainder.

Migration defaults existing accumulators to zero. Previously committed settlement replay returns its unchanged history before touching any carry. The first newly committed B6 settlement contributes prospective fractions; this includes delayed sources settled for the first time after cutover using their own pinned source config. Config versions never share remainders, even if their rates happen to match. Undistributed fractions remain subunit entitlement, not spendable Treasury; no fraction is rounded up at Season close. Existing close/barrier/drain rules remain. See the review for proof, rollout and historical-policy boundaries.
