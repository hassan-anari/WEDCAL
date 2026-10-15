# B3 backend handoff

Draft PR: https://github.com/hassan-anari/WEDCAL/pull/28.
Branch: `ai/b3-backend-integration`, targeting `main`.
Paired frontend: https://github.com/hassan-anari/FROCAL/pull/4, `ai/b3-frontend-integration`,
inspected head `0edabe530ab0e373ef61ea29b1e2b9b07f7e2be8`. Its description still pins the initial
incremental backend contract `b443eee`; FROCAL must repin to the final backend SHA and current
implemented schemas/fixtures before completing B3. Final immutable SHA and exact-head CI evidence
will be recorded in the PR and `docs/evidence/B3_VALIDATION.md`; never infer acceptance from this
handoff alone. No merge, deployment, production mutation/reset, B3 activation or live mass send.

## Implemented scope

P-05/P-06/P-07 backend integration, plus the owner's explicit B3 session/banner/bot/broadcast
extensions. Reused existing economic/domain services. New typed contracts/canonical state;
Daily cycle; mission transient recovery and repeating/disabled-verified obligations; Typing
resume; actual referral eligibility/invitees/tiers/earnings; actual Creator/Network summaries;
exact-generation release/ownership and fenced-client automatic reacquisition prevention;
dedicated disabled-by-default banner CMS and secure image delivery; durable English welcome;
strict private Bot Admin; user/group/both broadcasts and consent; shared throttling/retry fixes.

Read [B3_BACKEND_CONTRACT.md](B3_BACKEND_CONTRACT.md) and
[the security matrix](B3_BOT_ADMIN_SECURITY.md). All listed APIs are implemented, not speculative.
Exact schema: `docs/fixtures/b3/openapi.json`. Synthetic response fixtures: `earn-network.json`,
`creator-network-active.json`, `errors.json`, `banners.json`, `session-switch.json`,
`session-recovery-latency.json`, `start-welcome.json`, `broadcast-audiences.json`,
`broadcast-retry-*.json` and `broadcast-cancel-uncertain.json`.

Inventory by domain:

- `apps/core/b3.py`, rewards/bootstrap/schemas, Profile overview: shared Player-locked canonical
  read metadata and stable B3 error/schema helpers.
- `apps/daily`, `missions`, `typing_game`, `referrals`, `network`: typed request/read/claim
  contracts, API completion and regression fixes; existing settlement primitives retained.
- `apps/base_tap`: ownership read state, safe release, durable fencing history surviving lease cleanup, explicit-recovery semantics and paired tests.
- `apps/players/avatars.py`: octet-stream MIME admission hotfix parity; existing decoding, bounds, SSRF controls and JPEG sanitization retained.
- `apps/content`: banner models/admin/storage/validation/read API and storage-fake tests.
- `apps/telegram`: authenticated ingress provenance, private welcome, stricter start normalization,
  shared sending/per-chat budgets and private callback ingress dispatch.
- `apps/notifications`: durable admin state, consent, group targets, fair materialization/dispatch,
  metrics, cancellation, temporary retry and ambiguous-worker-loss handling.
- `scripts/export_b3_openapi.py`, existing OpenAPI export, CI quality drift gate and docs/fixtures.

New forward migrations:

1. `content/0001_initial`: independent banner asset/content tables, schedule constraint/index.
2. `telegram/0008_telegraminboundupdate_webhook_authenticated_at`: nullable provenance column;
   historical ingress is preserved, never retroactively authenticated for admin.
3. `base_tap/0003_tapgameplaysession`: durable player/session generation markers; lease cleanup
   preserves fencing history with monotonic updates. No existing economic row is rewritten.
4. `notifications/0003_broadcastcampaign_estimated_groups_and_more`: audience/cursor/metrics,
   message options, nullable player + real group target with exactly-one-target check and
   campaign-group uniqueness, private operator/session/one-use interaction state, consent history.

No data reset/data rewrite or economic config migration. Historical RewardLedger, Season Points,
GameConfig versions, existing Players and referral rules remain intact.

## Transactions and recovery

Tap retains Season barrier → Player → burst state order. Takeover compares generation under the
Player lock. Old release has no effect on successors. Daily/Mission/Typing continue through the
canonical settlement primitive and their uniqueness/row locks. Claim response snapshots share the
transaction. Bootstrap/Profile/state reads use the same Player lock. Concurrent Daily/Mission
settlement during takeover is tested with preserved 150 historical points and Season accounting.

Broadcast materializers lock campaign/cursors; workers use campaign then recipient SKIP LOCKED.
PROCESSING is the dispatch reservation boundary. Cancel skips pending/retry rows, not dispatched
rows. Expired dispatch reservations become UNCERTAIN; there is no unsafe automatic resend.
Consent revisions prevent old materialized targets from being revived by later re-consent.
Admin interaction consumption, state change, campaign queue audit and response outbox commit with
the authenticated inbound update. No privileged synchronous webhook business logic was added.

## FROCAL integration instructions

1. Pin the exact backend commit, its OpenAPI and all fixtures together. Keep fake frontend data out
   of production fallbacks. Every game API uses signed Telegram credentials and scoped query keys.
2. Use real Daily/Mission/Typing state and mutation responses. Reconcile `canonical`, including
   cursor ordering, across Base/Header/Earn/Profile; invalidate relevant reads after claim.
3. Restore Typing from active_attempt_detail. Never compute authoritative reward XP in the browser.
   Keep the same bounded idempotency keys through lost responses and wallet retries.
4. Use server referral code/link/tier values; show null link as unconfigured. Consume real owned
   group/activity/earned totals. Unique qualified speakers are not total Telegram members.
5. Use explicit switch UI for another owner/moved session, read generation, then takeover:true
   only after a click. Never automatically retry takeover after GENERATION_STALE. Suspend on
   network/storage/auth uncertainty, preserve the outbox and recover immutable receipts first.
6. Add best-effort exact-generation release after flushing. Do not depend on WebView closure,
   pagehide or passive 120-second expiry for fast recovery. Poll/read ownership on resume.
7. Render banner only when the server returns one; hide fully on null/image failure. Preserve
   the approved 1200×260 / 60:13 placement; frontend flags cannot activate advertisements.
8. Pair Android/iOS/Desktop first acquire, switch, close/reopen, offline, lost-response, stale
   release, cooldown and unrelated-claim scenarios. Backend local latency is not WAN certification.

No frontend source was changed. FROCAL's agent owns UI binding, removal of sample stores and
physical Telegram acceptance. `read-season-brief` remains explicitly unavailable pending PH-043
approved content/completion evidence; no fake reward verifier was invented. B4/B5/B6 stay deferred.

## Compatibility and safe rollout / rollback

All new response fields/routes are additive. Tap 2.0 and Bootstrap 1.1 stay compatible with old
fixtures/clients; optional schema fields accommodate old snapshots. Published transport/economic
policy remains 50 taps/burst, 10-second cooldown, 120-second leases; no per-touch requests, day cap,
DLX mint or passive Tap referral share. Both stored earning projections still derive from the ledger.

Operators must review migration SQL against actual table size/locks before rollout, especially
recipient check/unique indexes. Apply additive migrations and deploy API plus all workers before
any B3 bot switch/audience is enabled. Old workers must not consume new GROUPS/BOTH campaigns:
legacy materialization code does not understand them. Defaults `B3_BOT_WELCOME_ENABLED=false`,
`B3_BROADCAST_AUDIENCES_ENABLED=false`, empty TELEGRAM_ADMIN_IDS and disabled banners prevent
accidental activation. Configure roles/HTTPS app URL/storage/link hosts using deployment secrets;
never put credentials in frontend. The banner S3 account may reuse existing SDK credential
infrastructure, but uses its own bucket/settings and does not require avatar-cache activation.

Disable switches to stop new bot issuance/queueing; already durable deliveries/campaigns require
explicit cancellation or controlled drain before older workers return. Switches do not erase
queued obligations. Disable/deactivate banners independently. Do not roll schema back while group
recipients exist; retain additive tables, receipts, ledgers and audit. Never re-enable legacy Tap
or replay accepted economic requests as rollback. No rollout or rollback operation was executed.

## Evidence and remaining gates

Initial discovery pinned both HEADs, read repository instructions, canonical Source of Truth,
economy config, cross-repo roadmap/backlog and actual frontend/domain sources. FROCAL has no root
AGENTS.md at its pinned HEAD. Historical docs referring to old repository names/older B2 draft
status are superseded by the owner's current mission and actual merged main, not silently changed
into claims of fresh server verification.

Local full PostgreSQL regression, opt-in B2 benchmark, new recovery latency, quality/schema/static
checks and exact-head GitHub Actions evidence are tracked in the validation record/PR. No mocked
outbound Telegram call is reported as live delivery proof. No physical-device trace or production
incident logs were available to reconstruct the owner's precise live lockout sequence; source-level
recovery defects and paired server transitions are reproduced. Independent review must inspect the
actual diff and required checks, then STOP awaiting owner review. No next batch is authorized.


## Owner-requested avatar cache hotfix parity

The owner reports the live Telegram → Backblaze B2 → API → Mini App path already passed and that
some real Telegram JPEGs use `application/octet-stream`. B3 commits that narrow MIME allowlist
addition in `apps/players/avatars.py`. Real bytes still pass Pillow format/pixel validation and
JPEG re-encoding; response-size/time/path/IP/redirect protections are unchanged. Regression cases
exercise valid octet-stream JPEG, invalid bytes, HTML labelled octet-stream, explicit HTML MIME,
and existing JPEG/PNG/WebP, including metadata stripping and thumbnail bounds. No new avatar
architecture, credentials, storage configuration, deployment or live read/write was performed.
