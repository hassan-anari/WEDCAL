# B3 backend integration contract — incremental delivery

Branch: `ai/b3-backend-integration`. Base WEDCAL `a213c209f04411fdecc63ef4d78516f60856ee44`;
frontend audited at FROCAL `ba2cdabcf30fe3d99fd8f43159c1422f2c0175d4`.
This document distinguishes executable contracts from pending work. Nothing here authorizes rollout.

## Implemented and ready on this branch

All game APIs use server-verified Telegram `Authorization: tma <initData>`.
Tap protocol 2.0 routes and immutable receipt evidence remain compatible with
[the B2 contract](B2_TAP_V2_CONTRACT.md). Additive fields:

`GET /api/v1/base/v2/tap-state?session_id=<UUID>` and every Tap snapshot now include:

- `ownership.status`: `AVAILABLE`, `OWNED`, `ANOTHER_DEVICE`, or `SESSION_MOVED`.
- `ownership.active_until`: server lease expiry or null.
- `ownership.explicit_switch_required`: whether explicit user intent is required.

Initial entry: request state, then `POST /api/v1/base/v2/tap-leases` with
`{"session_id":"<UUID>"}`. Another owner produces `ACTIVE_SESSION` (409).
An old fenced session produces `LEASE_REVOKED` (409); automatic acquisition is forbidden,
including after the successor expires. Show the moved-session state, never a network error.

Explicit switch: read current state and send the existing lease endpoint
`{"session_id":"<UUID>","takeover":true,"expected_generation":<observed integer>}`.
A concurrent winner makes the loser `GENERATION_STALE`; require a new user decision,
never automatically retry takeover. The transaction fences the prior generation immediately.
Cooldown remains authoritative; a `COOLDOWN` response requires waiting until its server deadline
before retrying the same intentional action. Burst allowance never resets on switch.

`POST /api/v1/base/v2/tap-release` accepts exactly `lease_id` (UUID), `session_id` (UUID),
`generation` (positive integer). It returns a current Tap snapshot. Authentication and existing
player/session throttles apply. Release is best effort after flushing; suspension/closure cannot
be relied on to deliver it. A stale release cannot revoke a newer generation.
No heartbeat and no per-touch requests were added. Passive expiry remains 120 seconds under the
published config; explicit takeover is the recovery path for the 10–20 second UX target.
Latency measurements and the complete paired-device matrix are pending, not claimed passed.

Pending batches: replay their original immutable request/key before new earning. A committed
receipt is returned even after takeover/expiry; never reprice or re-key an old batch. Revoked or
expired unaccepted input must not be credited. Preserve unresolved local evidence. Authentication
errors retain `AUTH_*`; network/DB outages retain retryable `TEMPORARY_UNAVAILABLE`.
Storage failure is a frontend-local state, never equivalent to ownership or authentication failure.

Fixtures: [session-switch.json](fixtures/b3/session-switch.json) is generated from a synthetic
PostgreSQL test. UUIDs and database identifiers are example identities, never production data.

The shared notification delivery path now retries `local_rate_limited`, `rate_limited`, and
`budget_unavailable`, respects Retry-After, adds bounded jitter, and does not exhaust delivery
attempts on admission refusals. Ambiguous sends remain `UNCERTAIN`, without automatic resend.

## Existing endpoints; B3 completion pending

These routes exist at the inspected HEAD. They may be used to inspect existing behavior, but
B3 typed additions, cross-page settlement snapshots and complete fixtures are not yet finalized.

| Surface | Existing routes (all under `/api/v1`) | Remaining B3 contract work |
|---|---|---|
| Daily | `GET /daily`, `POST /daily/claim` | Typed cycle/claim/canonical progression and errors |
| Missions | `GET /missions`, `POST /missions/{key}/start`, `/verify`, `/claim` | Availability, transient verification errors, typed snapshots |
| Typing | `GET /typing`, `POST /typing/attempts/start`, `/attempts/{uuid}/submit`, `/claim` | Resume phrase, expiration/replay errors, typed balance and progression |
| Referral | `GET /referrals` | Invitee statuses, activation eligibility, earnings read model |
| Creator/Network | `GET /network/groups`, `POST /network/groups/intents`, `/groups/{uuid}/reverify` | Group metrics, earning summaries, eligibility and state recovery |
| Base/Header/Profile | Bootstrap and profile overview | Coherent canonical progression after every reward; no local minted XP |
| Banners | No dedicated API yet | Entire disabled-by-default CMS/read contract pending |

Do not implement clients against invented banner or summary schemas. Updated schemas/fixtures
will be published only with their implementation.

## Backend-owned work pending

Private `/start` welcome and durable keyboard delivery; actor-bound private Bot Admin;
explicit group broadcast opt-in, user/group/both materialization, independent metrics,
per-chat budgets/fairness and full security regression evidence. No live sends are authorized.

## Dependency and scope matrix

| Page | Existing and ready | Existing but incomplete | Missing in B3 | Future/reserved |
|---|---|---|---|---|
| Base | B2 Tap settlement/receipt/config | Ownership UX and Daily/mission summaries | Managed banners | War alerts |
| Earn | Canonical Daily/mission settlement | Typed state/errors and claim reconciliation | None of its economy primitives | B4 full history UX |
| Typing | Issued attempts/server scoring/wallet | Resume, expiration/error/read contracts | None of its economy primitives | ML anti-cheat |
| Profile | Lifetime/level/Dexo overview | B3 reward refresh contract | None | B4 collection/achievements |
| Header | Bootstrap progression | B3 canonical refresh/cache ordering | None | B4 notification UX |
| Referral | Opaque code/first-touch/verification/sharing | Invitee/eligibility/earnings reads | None of its economy primitives | New economics |
| Creator | Intent/token/admin verification/settlement | Group status/earning summaries | B3 UI read coverage | Ad networks |
| Network | Durable activity/anti-fraud/Speaker config | Real aggregate presentation | No fabricated activity | Clan B5 UI |

Preserve all published config versions, historical ledgers and legitimate progression. Season
Points and Lifetime XP are distinct views of canonical events. DLX is separate off-chain
eligibility-based allocation. Base Tap remains excluded from passive referral sharing.
