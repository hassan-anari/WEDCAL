# B4 backend integration contract

Contract owner: WEDCAL, branch `ai/b4-backend-integration`. Consumer: FROCAL B4.
Merged baseline WEDCAL `1fa937af4628a10cec384b711210755ff28a6a8a`;
FROCAL `ae91624f23a48d8a9973154520284706a9df1c21` (verified via GitHub).
Draft implementation: API schemas/fixtures below are exported from actual routers and isolated PostgreSQL tests. CI and final paired acceptance remain required. No activation approval.

Authentication remains signed Telegram initData (`Authorization: tma …`). Personalized
responses are private/no-store. Reuse B3 canonical progression and receipt ordering.

| Surface | Existing routes | Implemented behavior / frontend requirement |
| --- | --- | --- |
| Profile | GET `/profile/overview`, `/achievements`, `/collection`; POST `/review`; PUT `/customization` | Bind real catalog entitlement/progress and customization; retain Coming Soon wallet/account. No local production unlock or equip authority. |
| Notifications | GET `/notifications?cursor&limit`; POST `/{uuid}/read` | Safe server-selected internal deep links, unread/read-all metadata. Never navigate arbitrary payload URLs. |
| Arena | GET `/arena/rankings/players`, `/rankings/clans` with offset/limit, `/state`, `/dlx` | Typed pages, explicit Season selection/archive behavior, projection consistency, private avatar URLs, actual confirmed off-chain allocations. Locked S2; no USD price or claim. |
| Typing | B3 attempt/start/submit/claim and resume | Authenticated paginated real run history at GET `/typing/history`; schema and fixtures below. |
| Claims | B3 Daily/Mission/Typing canonical results | Add confirmed ledger evidence and server progression before/after for existing reveals; duplicate response must never trigger a second reveal. |
| Referral/groups | Existing invitee/group pages and reverify | Optional private image delivery, automatic bounded status refresh; initials on unavailable images. No Bot API/file URL or credential exposure. |
| Creator consent | Explicit consent endpoint | Intentional verified Add Bot authorization, current creator/bot authority, preserve revocation. No historical enrollment; broadcast still requires operator confirmation. |
| Bot Admin | Durable private delivery and scanner | Immediate after-commit Celery dispatch; prompt callback ACK; scanner retained for recovery. |
| Welcome | Versioned disabled-by-default welcome | Versioned Admin content/keyboard with validation, preview, operator-only test send, activation/rollback. Public switch remains disabled without explicit owner authorization. |

Mission publication must match effective GameConfig; malformed historical definitions must
not break Bootstrap. Verified obligations retain original version/config/reward/idempotency.
No XP/DLX economics change is authorized. OpenAPI and synthetic HTTP fixtures are in
`docs/fixtures/b4/`, generated from actual routers/tests. FROCAL must pin the final backend
SHA and run paired tests, including auth, empty/error/archive and cross-player isolation.


## Implemented B4 wire additions

Paired consumer: https://github.com/hassan-anari/FROCAL/pull/5.
Baseline inspected consumer head `fe7e93c56303d4b02125a974fa29efde9d59031e`.
Keep its historical B3 source/fixture pin intact; add a separate exact B4 pin.

- `GET /typing/history?period=all|current&cursor=<positive integer>&limit=20`:
  `items`, `next_cursor`, `period`, `period_key`, `period_start`, `period_end`.
  Default `period=all` preserves historical pagination; its period key/boundaries are null.
  The existing **TODAY** UI MUST request `period=current`. The server resolves the effective
  **Typing** `reset_timezone` independently from Daily and matches persisted attempt
  `reset_period` to that key. Returned UTC boundaries and key are authoritative; every run
  also carries its actual `reset_period`. Do not infer local dates or reuse Daily boundaries.
  An attempt started before reset remains in its original period even when submitted after reset.
  Current-period cursor anchors must belong to this player and be submitted: invalid/foreign
  anchors are REQUEST_INVALID/422. A cursor from a previous period returns
  TYPING_PERIOD_CHANGED/409; discard accumulated TODAY pages and fetch current page one.
  This is cache reconciliation inside the existing view, not a new visible control/state.
  Scope cached pages by player, period key and config/Season as applicable; revalidate on the
  existing focus/resume/reset path. Empty current period remains an honest empty list.
  Limit 1–100. Descending internal run cursor is scoped to authenticated player; stable run
  identity is `attempt_id` UUID. Each submitted run includes `status`, `started_at`,
  `submitted_at`, `accuracy_bps`, `wpm`, `reward_amount`, source `season`, `config_version`,
  and `claimed`, plus nullable `score`. Score preserves the existing frontend display metric
  `Math.round(accuracy_percent * Math.max(1, wpm))`, computed with server scoring and integer
  half-up rounding. It is NOT wallet credit or XP and is never an economic input. Legacy
  missing scoring yields null rather than invented performance. Submit also returns this score.
  No typed text, phrase answers, tokens, other players or invented runs.
  STARTED/abandoned unsubmitted rows are omitted; rejected/suspicious submitted runs remain
  real history with authoritative zero reward. A run's reward is Typing-wallet credit,
  not evidence of canonical XP settlement. `claimed` reflects actual credit-to-claim linkage.
- Daily/Mission/Typing successful claims add nullable `reward`: `event_id`, `amount`,
  `ledger_ids`, `source`, `created`, `progression_before`, `progression_after`. Amounts are
  sums of confirmed RewardLedger rows; progression uses the same server policy and Player
  lock as canonical settlement. No ledger means `reward:null` (empty Typing claim).
  Reveal only a newly created, positive confirmed reward once per event; persist/dedupe event
  identities across uncertain responses. Duplicate claims return original ledger identity with
  `created:false`. Canonical after-state owns balances; do not increment a client XP store.
- Invitee and owned-group items add `photo_url` (empty string when unavailable). Invitee
  refresh works for `/start`-only Players without Mini App photo metadata. Images use the
  existing private avatar delivery path or new opaque `/profile/group-avatar/{UUID}` capability.
  No Bot API credentials, file IDs or direct Telegram file URLs appear in new contracts.
- `POST /network/groups/intents` accepts `{authorize_group_messages:false}` by default.
  Pass true only when the existing intentional Add Bot action clearly authorizes group
  messaging. Token is creator-bound, one-use and expiring; actual bot/creator Telegram
  authority must verify before a new subscription can be enrolled. Existing or explicitly
  revoked subscriptions are never automatically replaced/re-enabled. No manual broadcast
  toggle or verification action is required in the Mini App. GET owned group pages schedules
  due verification with a bounded reservation; existing scanner recovers dispatch loss.
- Notifications add `deep_link:{path}` or null, chosen by canonical source domain from the
  existing internal route allowlist. CLAN events point to `/clan`; arbitrary payload URL,
  query, script, protocol-relative or unknown route is never navigable. Page adds authoritative
  `unread_count`. `POST /notifications/read-all` returns `updated_count`, `unread_count`.
  Read/read-all are authenticated, actor-scoped and idempotent. BOT_PRIVATE stays excluded.
- Ranking queries add optional `season=<code>`. Page adds `season`, `season_status`, `archived`,
  `as_of`, `total_count`, `next_offset`. Existing items/own_standing/offset/limit are retained.
  Live Redis rank/score/generation are captured atomically, compared with PostgreSQL and
  fail closed on known pending projections/drift. CLOSED/ARCHIVED rank and score use immutable,
  indexed final standings. Empty population has no fabricated podium or self rank.
- `/arena/state` adds current `season`/null and actual ACTIVE/CLOSED/ARCHIVED `seasons`
  (latest 50), and false wallet/account flags. `/arena/dlx?season=<code>` returns confirmed,
  held, forfeited, net_confirmed from one SQL snapshot of allocation/adjustment ledgers.
  Claimable stays 0; claim/transfer/trade/on-chain flags false; price UNAVAILABLE.
  No visible archive selector or DLX card is authorized by this backend contract.

Profile catalog/entitlements/customization remain the existing typed APIs. Progression and
achievement evidence are server-owned; reconciliation grants no XP. First Verified Activity
has no approved secondary active-day counter: do not fabricate day progress. Cosmetics backed
by active Clan roles now exclude ended Seasons. Current streak uses actual Daily reset policy;
career maximum/unlock history persists. Wallet/account and S2 Warfront remain unavailable.

## Errors and compatibility

B4 retains B3 `{code,detail,retryable,retry_after_seconds}` and B1 auth/initialization errors.
New errors: `PROFILE_UNAVAILABLE`/503; `CUSTOMIZATION_INVALID`/422;
`RANKING_UNAVAILABLE`/503; `RANKING_RECONCILING`/503 (Retry-After: 2);
`SEASON_UNAVAILABLE`/409; `NOTIFICATION_NOT_FOUND`/404;
`TYPING_PERIOD_CHANGED`/409 (restart current-period pagination). Unsupported pagination is REQUEST_INVALID/422 (Ninja validation
on newer routes may retain B1 request_id envelope). Error detail never returns raw exceptions.
All personalized reads/mutations are private/no-store and vary by Authorization; image bytes
retain private bounded caching. Existing clients ignore additive fields; request paths and
existing mutation shapes remain valid. Old B3 pins remain historical compatibility evidence.

## Bot operations and Dynamic Welcome

Outgoing durable TelegramDelivery rows schedule one Celery task after the outermost commit.
Retries schedule from durable due time; the periodic scanner remains recovery/retry truth.
Rollback emits no task. Row leases, SKIP LOCKED claims and terminal ambiguity prevent duplicate
sends. Callback ACKs have an independent durable row and dispatch immediately after commit.
Operator permissions/allowlist are rechecked before a queued private panel/test send. Group
broadcast dispatch rechecks subscription revision/revocation and current bot/authorizing creator
Telegram authority. Already dispatched/uncertain operations cannot be recalled or auto-resent.
Actual broadcasts still require authorized operator confirmation and audit; no economic rules change.

WelcomeTemplate stores immutable versioned validated Telegram HTML and multiple URL/Web App
keyboard rows. Admin supports exact preview, explicit signed confirmation for activate/rollback
and own-operator-only test-send, distinct permissions and OperatorAuditEvent history. Public
Welcome requires BOTH existing `B3_BOT_WELCOME_ENABLED` and new `B4_PUBLIC_WELCOME_ENABLED`
(default false), plus an active version. No migration creates an active template. Activation
sends nothing; canonical `bot-welcome:{player}` deduplication survives template version changes.
Testing this batch never contacts real Telegram users.
