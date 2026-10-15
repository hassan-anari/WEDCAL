# B3 backend integration contract

Branch: `ai/b3-backend-integration`. Draft PR: https://github.com/hassan-anari/WEDCAL/pull/28.
Backend baseline: `a213c209f04411fdecc63ef4d78516f60856ee44`.
Frontend audited: `ba2cdabcf30fe3d99fd8f43159c1422f2c0175d4` in `hassan-anari/FROCAL`.

This revision supersedes the initial incremental contract. Routes below are implemented on this
branch. [OpenAPI](fixtures/b3/openapi.json) is exported from actual Ninja routers; it is not a
proposed schema. [Exact HTTP fixtures](fixtures/b3/earn-network.json) come from synthetic
PostgreSQL/API tests. No fixture is production economy data, and no code publication enables
production rollout.

## Authentication, availability and shared progression

Use `Authorization: tma <raw signed Telegram initData>` on game APIs. Never send a Telegram user
ID as authorization. Cache scopes must include authenticated player, Season and config version.
All personalized responses are private/no-store. Public banner image bytes have separate caching.

`canonical` on Daily/Mission/Typing claim results, Bootstrap, Profile overview and Network summary
contains `server_time`, `player_id` (Telegram identity as a string), `season_id` (Season code or null),
`config_version`, `receipt_cursor`, `progression`, and `season_points`.
`progression` uses the existing Bootstrap fields: `lifetime_points`, `level`, `current_threshold`,
`next_threshold`, `points_into_level`, `points_remaining`, `dexo_stage`.
`GET /api/v1/rewards/state` returns this object directly. These reads serialize on the same Player
lock as settlement; claim APIs retain their domain transaction through the canonical snapshot.

Use the complete server projection after a successful mutation. Do not add reward XP to a separate
local production store. Compare per-player `receipt_cursor` to discard older economic snapshots;
for Tap also retain its independent `revision` and generation. A Daily reward can advance the
receipt cursor without changing Tap revision. Do not treat a Tap cursor/generation as ordering
proof for an unrelated query. Invalidate/re-read Daily, Missions, Typing, Bootstrap, Profile and
relevant Network/referral data after claims. Preserve pending Tap evidence during those refreshes.
Bootstrap/Profile `canonical` and Tap `ownership` are optional in schemas for old-server/fixture
compatibility; this backend returns them. Absence on an older server is an availability/version
boundary, never permission to mint local rewards.

Lifetime XP persists across Seasons. Season Points are the current Season projection of the same
canonical reward events. DLX allocation is separate, off-chain and eligibility-based; these APIs
never convert XP into allocated DLX or offer withdrawal.

### Errors

B3 domain errors use `{code, detail, retryable, retry_after_seconds}`. `retry_after_seconds` may be
null; respect HTTP `Retry-After`. Authentication preserves B1 `{code, detail, request_id}` with
`AUTH_MISSING`, `AUTH_INVALID`, `AUTH_EXPIRED`, `AUTH_UNAVAILABLE`. Initialization/configuration
outages preserve `INITIALIZATION_UNAVAILABLE`/503. Tap retains its B2 error format, including
`retry_after_ms`. Middleware throttling uses `RATE_LIMITED`/429 and a Retry-After header.

| Condition | Code / HTTP | Frontend action |
|---|---|---|
| Invalid request | `REQUEST_INVALID` / 422 | Correct input; no reward retry loop |
| Daily disabled | `DAILY_UNAVAILABLE` / 409 | Hide/disable claim, show unavailable |
| Mission disabled/misbound/unimplemented | `MISSION_UNAVAILABLE` / 409 | Do not invent completion |
| Mission not verified | `MISSION_NOT_READY` / 409 | Refresh/start/verify |
| Invalid token or verification identity | `MISSION_VERIFICATION_INVALID` / 422 | Restart valid mission flow |
| Telegram membership API transient failure | `TELEGRAM_VERIFICATION_UNAVAILABLE` / 503 | Retain progress/token; retry after delay |
| Typing unavailable | `TYPING_UNAVAILABLE` / 409 | No local phrase/reward fallback |
| Missing/bad bounded key | `IDEMPOTENCY_KEY_REQUIRED` / 422 | Retain/generate action key before request |
| Attempt quota used | `QUOTA_EXHAUSTED` / 429 | Use Typing `next_reset` |
| Unknown/foreign attempt | `TYPING_ATTEMPT_NOT_FOUND` / 404 | Do not expose another player's state |
| Invalid typed input | `TYPING_SUBMISSION_INVALID` / 422 | No browser XP accepted |
| Wallet cannot settle | `CLAIM_UNAVAILABLE` / 409 | Refresh, preserve claim key |
| Player restricted | `PLAYER_RESTRICTED` / 403 | Pause reward actions |
| Season unavailable | `SEASON_UNAVAILABLE` / 409 | Refresh availability; preserve evidence |
| Creator disabled/limit/restriction | `CREATOR_UNAVAILABLE` / 409 | Render server eligibility |
| Bot link not configured | `BOT_UNCONFIGURED` / 503 | Disable add-bot action |
| Unknown/foreign Creator group | `GROUP_NOT_FOUND` / 404 | No ownership fallback |
| Group consent verification unavailable | `GROUP_AUTHORIZATION_UNAVAILABLE` / 409 | Reverify Telegram authority; no opt-in yet |

## Implemented Earn APIs

All paths below are prefixed `/api/v1`. Exact request/response names and nullability are in
OpenAPI and `earn-network.json`.

| Method/path | Contract |
|---|---|
| `GET /daily` | `DailyOutput`: claimable/already_claimed, current_streak, cycle_day, reward_amount, full reward_cycle, period_start/end, next_reset, last_claim_time |
| `POST /daily/claim` | Empty object. `DailyClaimOutput`: created, claim_period, streak, cycle_day, reward_amount, claimed_at, updated daily state, canonical |
| `GET /missions` | Array of `MissionOutput`, including family/repeat/reward, state, availability, destination, opened/verified/claimed timestamps |
| `POST /missions/{key}/start` | Empty object. Returns version/state, open_token, destination_url, opened_at |
| `POST /missions/{key}/verify` | `{open_token: string|null}`. Returns state and verified_at; membership proof and dwell are server-owned |
| `POST /missions/{key}/claim` | Empty object. Returns created/state/version/reward/claimed_at and canonical |
| `GET /typing` | Attempts used/remaining/limit, next_reset, claimable_balance, best_wpm/accuracy_bps, active_attempt ID and active_attempt_detail |
| `POST /typing/attempts/start` | Empty object; bounded `Idempotency-Key` (1–100 characters). Returns created, attempt_id, phrase key/version/text, started_at, expires_at, challenge_seconds |
| `POST /typing/attempts/{uuid}/submit` | `{typed_text: string}` (max 4096). Returns immutable server status/scoring/reward and updated typing state |
| `POST /typing/claim` | Empty object; bounded `Idempotency-Key`. Returns created/amount/claimed_at, updated typing state and canonical |

Daily uses the published config's UTC reset, seven-day reward cycle and continuing consecutive
streak. Missing a whole period resets the cycle/streak. Natural player+period uniqueness makes
concurrent/repeated claims exactly once. Daily never limits Base Tap.

Mission states are `AVAILABLE`, `IN_PROGRESS`, `REWARD_READY`, `CLAIMED`, `UNAVAILABLE`.
UI labels may map CLAIMED to Completed, REWARD_READY to Reward ready. Only published definitions
bound to the effective config appear. Disabled/out-of-schedule definitions are omitted; an
unimplemented `read-season-brief` verifier is explicitly unavailable with
`PRODUCT_EVIDENCE_PENDING` (existing PH-043 owner-content dependency). No fake verifier was added.
TIMED_EXTERNAL tokens are player/definition/period-bound. Retrying start preserves the original
server opening time; returning before dwell finishes remains IN_PROGRESS. TELEGRAM uses actual
membership, including restricted/is_member semantics; transient failures do not claim or reset
progress. INTERNAL uses actual profile review/valid Typing evidence. Verified obligations can
still settle after a definition is disabled; source config/time and historical rewards survive.
Repeated daily missions no longer select a previous day's CLAIMED row.

Typing uses the server-issued phrase, config-defined 30-second window, full-length completion,
server accuracy/WPM and configured thresholds. The browser supplies text, never authoritative XP,
accuracy, WPM, elapsed time or Season. Extra browser score fields remain ignored for B1/B2
compatibility. Identical or conflicting repeats of a completed attempt return its original result
and cannot create another credit. Invalid completed runs consume quota; abandoned expired starts
and technical failure do not. `active_attempt_detail` enables reload/resume of the same phrase.
Rejected/suspicious runs receive no claimable credit; suspicious is not an automatic ban.
Claim transfers the supported current wallet through the canonical ledger exactly once. Daily
reset does not erase the wallet; existing Season-close source-Season settlement remains intact.

## Implemented Referral and Creator/Network APIs

| Method/path | Contract |
|---|---|
| `GET /referrals` | Opaque immutable code; canonical Telegram link (null if bot username unconfigured); enabled; pending/verified counts; current/next/all tiers; earned_points; own attribution_status/activation eligibility; configured bonuses |
| `GET /referrals/invitees?offset=0&limit=30` | Actor's real invitees: name, status, attribution/verification timestamps, activation gates. limit 1–100; next_offset/null |
| `GET /network` | Enabled/status eligibility, actual active/group counts, max_active_groups, can_register, speaker flag, settled earned_points, settlement_window_seconds, canonical |
| `GET /network/groups?offset=0&limit=30` | Owned groups only, UUID/title/state, bot/creator verification timestamps, retry state, earned points, qualified characters, unique speakers, latest qualified activity and broadcast_opted_in |
| `POST /network/groups/intents` | Empty object. Authenticated, expiring startgroup_url and expires_at; no manual Chat ID input |
| `POST /network/groups/{uuid}/reverify` | Empty object. 202 `{accepted:true}` or 404; durable due time plus broker dispatch, recovered by existing periodic scanner |
| `POST /network/groups/{uuid}/broadcast-consent` | `{enabled:boolean}`. Separate explicit opt-in/revocation after Telegram group authority and bot send permission checks. Returns enabled/revision |

Referral first touch is immutable. Activation uses actual level, account age, qualification and
moderation facts. The existing asynchronous activation/share services remain authoritative.
There is no client-supplied inviter, activation or share amount. Tiers/bonuses come from published
config, not FROCAL's mock percentages. Base Tap remains excluded from passive referral sharing.

Creator states remain REVIEW/ACTIVE/ADMIN_REQUIRED/SUSPENDED/INACTIVE. Register using the
server's add-bot token, then reverify/refresh. Bot admin and creator authority use existing
services; admin loss pauses earning. Counts/earnings come from actual activity and settlement
rows, not group-size guesses. `unique_speakers` is lifetime qualified speakers observed by this
registration, not a total Telegram member count. No member count is fabricated. Network Speaker
rewards remain disabled under the published initial config. Creator rewards auto-settle; there is
no new fake Creator claimable wallet. The frontend must not offer an unimplemented Creator claim.
[Nonempty Creator fixture](fixtures/b3/creator-network-active.json) is generated through the real
activity/settlement engine, with synthetic speakers.

## Tap session recovery and switching — implemented

B2 routes, economic invariants and immutable receipts remain as documented in
[B2_TAP_V2_CONTRACT.md](B2_TAP_V2_CONTRACT.md). Added `ownership` on every Tap snapshot:

- `status`: AVAILABLE, OWNED, ANOTHER_DEVICE, SESSION_MOVED.
- `active_until`: current authorization's server expiry or null.
- `explicit_switch_required`: whether a user action is required.

1. Read `GET /base/v2/tap-state?session_id=<UUID>`. Initial acquire remains
   `POST /base/v2/tap-leases` with `{session_id}`.
2. Another active owner gives ACTIVE_SESSION/409. Show another-device state and a switch button.
3. Only after the user approves, read the current generation and POST the same lease route with
   `{session_id,takeover:true,expected_generation:<observed>}`. PostgreSQL fences the prior
   generation before returning the new lease. The remaining allowance and policy stay pinned.
4. A racing switch loser gets GENERATION_STALE/409. Read state and require a new user decision;
   never loop takeover automatically. A fenced session attempting normal acquisition gets
   LEASE_REVOKED/409 even after the successor's lease expires or lease metadata is purged;
   durable session-generation markers preserve that history.
5. `POST /base/v2/tap-release` accepts exactly `{lease_id,session_id,generation}`. Authenticated,
   rate limited, exact-generation best effort; returns a current snapshot. A stale release cannot
   revoke a successor. Flush/persist outstanding batches first. `pagehide`/closing/suspension and
   beacon delivery are not reliable release guarantees.
6. COOLDOWN is preserved across switching. A switch during cooldown may wait the existing
   10-second deadline and retry intentionally. Timers never override the database.

On suspend, network loss or uncertain time confidence, pause new local earning and preserve the
journal. On resume/reconnect, read state and replay original pending requests/keys. Committed
receipts survive expiry/takeover/lease cleanup. Never re-key/reprice unresolved input. Definitively
revoked/expired unaccepted input earns nothing; keep its rejection evidence in the local recovery
history. A lost response is recovered from the immutable receipt, not an inferred sequence cursor.

Storage failure is frontend-local STORAGE_RECOVERY_REQUIRED presentation; it is not a server
ownership error. AUTH_* requires credential renewal; TEMPORARY_UNAVAILABLE/network timeout
requires bounded retry; ACTIVE_SESSION means another owner; LEASE_REVOKED plus SESSION_MOVED
means this device was replaced. Do not collapse these into Connection interrupted.

There is no new per-touch request or heartbeat renewal. Passive expiry stays 120 seconds in the
immutable published policy. Always offer explicit safe takeover to meet rapid recovery targets.
FROCAL should poll ownership when visible (a bounded ~5-second read is suitable) and always check
on resume. A disconnected device cannot receive instantaneous knowledge of remote revocation;
PostgreSQL earning authority is fenced immediately, but local cached display is not proof of
continued earning permission. No claim of instantaneous cross-device offline notification is made.

[Session fixtures](fixtures/b3/session-switch.json), [latency measurements](fixtures/b3/session-recovery-latency.json).
Measurements are local service transactions, 20 samples/scenario, not WAN/mobile SLO certification.
The cooldown test advances server time; its 10-second economic wait is separate from measured DB
latency. FROCAL/device integration must measure the complete healthy-network click-to-recovery
path against the owner's preferred 10–15 seconds / approximately 20 seconds maximum.

### Root cause evidence from actual frontend/backend HEADs

FROCAL `integrations/api/tapV2.ts:118` always sends `takeover:false`; it exposes no explicit switch.
`tapV2/lifecycle.ts` suspends on pagehide but never releases the backend lease. The controller
retries ACTIVE_SESSION as reauthorization/paused; generic presentation does not distinguish this
from connectivity. Thus closing/switching can leave the backend's 120-second owner active while
other clients repeatedly try ordinary acquisition. B2 then permitted previously fenced sessions
to reacquire after expiry, creating the possibility of repeated ownership changes. In addition,
`tapV2/controller.ts` returns terminal before fetching state when a persisted journal contains a
blocked batch; restarting the controller alone reads the same journal again. FROCAL recovery must
reconcile those original requests/receipts and archive definitive rejection evidence explicitly,
without deleting unresolved economic evidence. B3 removes
that automatic reclaim and publishes the explicit recovery contract.
These source-level failures are reproduced by paired PostgreSQL tests. The exact owner's live
phone/desktop sequence cannot be reconstructed without its incident logs/device trace; no such
production read or physical-device verification was performed here.

## Promotional banners — implemented, disabled by default

`GET /api/v1/content/banners/BASE` returns `{"placement":"BASE","banner":null}` when no banner
qualifies. Render nothing: no image, placeholder or reserved layout height. Frontend flags alone
never activate content. An eligible banner contains UUID id, image_url, alt_text, destination/null,
destination_type (TELEGRAM/HTTPS/null), width/height and content digest version.
See [banner fixtures](fixtures/b3/banners.json).

Dedicated `content.PromotionalBanner` and `BannerAsset`, separate from economic GameConfig.
Enabled defaults false; start inclusive/end exclusive; priority descending, then creation/UUID.
Assets require approval from validated backend upload. Static JPEG/PNG/WebP only, max configured
bytes (default 1 MiB), 60:13 ratio, 600–2400 px width / 130–520 px height; normalized to 1200×260
WebP. Reject SVG/HTML/executables, animated/decompression-bomb/invalid images and oversized input.
Keys are SHA-256-based under `banners/`; metadata is removed by re-encoding.

Django Admin supports upload, preview, schedule, enable/disable, soft deactivation and audit links.
`content.add_bannerasset`, add/change banner and `publish_promotionalbanner` are distinct rights.
`OperatorAuditEvent` records create/publish/update/enable/disable/deactivate with before/after and
operator. Economic records/configs are untouched. Deletion in Admin deactivates, preserving history.

Storage uses dedicated BANNER_S3_* settings and SDK credentials; it does not depend on avatar-cache
activation. BANNER_ASSET_ORIGIN must be HTTPS. The API serves only approved matching UUID/digest
WebP bytes via a public immutable asset route, bounded reads and digest verification, with nosniff,
CSP and one-year versioned caching. No browser storage credentials or arbitrary reference URL.
Missing storage returns an empty banner; failed image delivery is 503/no-store, and frontend hides
that image slot. Destination URLs require approved exact HTTPS hosts; reject credentials, unsafe
ports/schemes, malformed paths, fragments, encoded/control/backslash tricks and redirect queries.
Only Telegram launch query keys are allowed on t.me. Configure trusted hosts explicitly.

## Backend-owned bot operations — implemented

With `B3_BOT_WELCOME_ENABLED=true`, private `/start` retains existing canonical identity and immutable attribution, then creates one
durable welcome delivery per player. Repeated starts and webhook retries do not reset progress or
spam duplicate welcomes. Approved English copy uses Telegram HTML `<b>` with no user interpolation.
The genuine web_app Play button and standard Community URL button share one row. Official app URL
is environment-configured (initial default https://dalexio-game.vercel.app/); Community is
https://t.me/Dalexio_Official. [Exact outgoing text/options](fixtures/b3/start-welcome.json).
DLX copy explicitly states eligibility, off-chain allocation and no guaranteed/withdrawable balance.

Bot Admin is inside the bot's private chat. New audience queueing and Bot Admin require
`B3_BROADCAST_AUDIENCES_ENABLED=true`; both B3 bot switches default false for safe rollout. `/admin` requires the verified webhook/persisted-update
provenance, environment numeric-ID allowlist, enabled BotAdminOperator mapping to an active internal
user with `notifications.manage_broadcasts`, genuine private chat with chat.id == from.id, and
actor-bound/unexpired/single-use interaction tokens. Recheck on every message and callback.
Groups, anonymous/bot/forwarded/mismatched actors and stolen/replayed/expired tokens cannot access
admin. No username or callback action payload authorizes anything. All privileged state transitions
and campaign actions are audited without secrets/full text in ordinary logs.

Workflow: menu → compose/edit plain text → Users/Groups/Both → exact outgoing preview plus separate
estimates → explicit one-use confirmation → progress/refresh/cancel. Sessions expire after configured
900 seconds and retain durable drafts across restart. `/admin` recovers the menu; safe abandon never
queues. Cancel skips pending/retry targets; already dispatched/uncertain messages are never resent.
Callback acknowledgements also use the durable shared delivery path. Private bot delivery records
are marked read and excluded from the in-app feed; they do not create blank game notifications. The panel exposes no rewards,
wallet, SQL or general database administration.

Users are eligible private contacts. Groups need explicit consent independent of Creator connection,
verified real group/admin identity, current bot membership/send permission, revocation and audited
revision. No fake Player represents a group. Existing BroadcastCampaign/Recipient/TelegramDelivery
services perform bounded restart-safe user/group materialization, independent estimates/counts,
round-robin campaign admission and bounded user/group shares within a batch. Progress includes
materialized/sent/retry_wait/failed/uncertain/skipped and oldest pending age per audience.
[Audience fixture](fixtures/b3/broadcast-audiences.json). Explicit local/Telegram/budget retry and
cancellation/uncertain fixtures are indexed in [the fixture README](fixtures/b3/README.md).

All Bot API calls share global/method budgets and 429 pause. Sending additionally uses a conservative
10 RPS sub-budget and per-chat spacing (private 1s; group 3s), leaving capacity for verification,
Creator and avatars. Sending fails closed on budget outage. Local throttling/429/budget unavailability
schedule retry with Retry-After, bounded backoff and jitter; they do not consume dispatch attempts or
become FAILED solely due to admission exhaustion. Retry queue age is bounded (default 24h), after
which still-throttled rows become SKIPPED/retry_deadline_exceeded. Permanent Telegram failures end;
ambiguous sends and expired dispatch leases become UNCERTAIN without automatic resend.
No actual Telegram mass sends occur in tests.

## Dependency and scope matrix

| Surface | Existing and ready | Existing but incomplete, completed in B3 | Missing, implemented in B3 | Reserved/pending outside backend code |
|---|---|---|---|---|
| Base | B2 settlement/receipts/config | Explicit ownership/release/recovery | Managed banners | FROCAL binding and physical-device acceptance; War deferred |
| Earn | Daily/mission economic primitives | Typed cycles/states/errors, repeating mission claim, canonical results | Shared state read | No new economy or local minting |
| Typing | Attempts/server scoring/wallet | Resume phrase, typed quota/result/claim contracts | Complete fixtures | ML anti-cheat deferred |
| Profile | Lifetime/level/Dexo | Player-locked overview and canonical state | Additive canonical metadata | Full B4 collection/Arena work |
| Header | Bootstrap | Additive canonical metadata and refresh ordering | Shared read contract | B4 notification UI |
| Referral | Code/attribution/activation/share | Invitee/eligibility/tier/earning reads | Paginated invitee contract | No new tier economics |
| Creator | Registration/admin checks/settlement | Status/metrics/eligibility/reverify recovery | Real activity fixture | No Creator claimable wallet |
| Network | Durable activity/anti-fraud/Speaker policy | Aggregate read coverage | Separate broadcast consent | B5 Clan UI |

Remaining dependency: PH-043 approved Season brief content/completion evidence; frontend integration
and real Android/iOS/Desktop acceptance are FROCAL/owner gates. No planned API is represented as
implemented in this document. No B4/B5/B6 implementation, merge, deploy or production mutation.
