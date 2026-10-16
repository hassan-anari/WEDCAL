# B4 backend contract — work in progress

Contract owner: WEDCAL, branch `ai/b4-backend-integration`. Consumer: FROCAL B4.
Merged baseline WEDCAL `1fa937af4628a10cec384b711210755ff28a6a8a`;
FROCAL `ae91624f23a48d8a9973154520284706a9df1c21` (verified via GitHub).
This initial inventory is not a completed implementation or activation approval.

Authentication remains signed Telegram initData (`Authorization: tma …`). Personalized
responses are private/no-store. Reuse B3 canonical progression and receipt ordering.

| Surface | Existing main | B4 work / frontend requirement |
| --- | --- | --- |
| Profile | GET `/profile/overview`, `/achievements`, `/collection`; POST `/review`; PUT `/customization` | Bind real catalog entitlement/progress and customization; retain Coming Soon wallet/account. No local production unlock or equip authority. |
| Notifications | GET `/notifications?cursor&limit`; POST `/{uuid}/read` | Safe server-selected internal deep links, unread/read-all metadata. Never navigate arbitrary payload URLs. |
| Arena | GET `/arena/rankings/players`, `/rankings/clans` with offset/limit, `/state`, `/dlx` | Typed pages, explicit Season selection/archive behavior, projection consistency, private avatar URLs, actual confirmed off-chain allocations. Locked S2; no USD price or claim. |
| Typing | B3 attempt/start/submit/claim and resume | Add authenticated paginated real run history. Exact route/schema will be committed with implementation; no speculative frontend binding. |
| Claims | B3 Daily/Mission/Typing canonical results | Add confirmed ledger evidence and server progression before/after for existing reveals; duplicate response must never trigger a second reveal. |
| Referral/groups | Existing invitee/group pages and reverify | Optional private image delivery, automatic bounded status refresh; initials on unavailable images. No Bot API/file URL or credential exposure. |
| Creator consent | Explicit consent endpoint | Intentional verified Add Bot authorization, current creator/bot authority, preserve revocation. No historical enrollment; broadcast still requires operator confirmation. |
| Bot Admin | Durable private delivery and scanner | Immediate after-commit Celery dispatch; prompt callback ACK; scanner retained for recovery. |
| Welcome | Static disabled-by-default welcome | Versioned Admin content/keyboard with validation, preview, operator-only test send, activation/rollback. Public switch remains disabled without explicit owner authorization. |

Mission publication must match effective GameConfig; malformed historical definitions must
not break Bootstrap. Verified obligations retain original version/config/reward/idempotency.
No XP/DLX economics change is authorized. Final OpenAPI and synthetic HTTP fixtures will live
in `docs/fixtures/b4/`, generated from actual routers/tests. FROCAL must pin the final backend
SHA and run paired tests, including auth, empty/error/archive and cross-player isolation.
