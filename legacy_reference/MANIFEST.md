# Legacy reference manifest

Classification meanings:

- **REUSE/ADAPT** — strong proven mechanism; transplant the idea and rewrite against current domain models/services.
- **ADAPT** — useful implementation detail, but old architecture/product coupling must be removed.
- **REUSE_TEST_IDEAS** — preserve the failure/regression scenario, not necessarily the old code.
- **REJECT** — listed only in `REJECTED_PATTERNS.md`; never port.

| Reference | Classification | Likely phases | Original source |
|---|---|---:|---|
| `telegram/miniapp_auth_core.py.txt` | ADAPT | 2 | `apps/telegram_airdrop/api/authentication.py` |
| `telegram/webhook_security_start.py.txt` | ADAPT | 2,5,7 | `services/fastapi_webhook/main.py` |
| `telegram/telegram_api_client.py.txt` | ADAPT | 5,7,10,11 | `services/fastapi_webhook/telegram_api.py` |
| `telegram/regression_tests.py.txt` | REUSE_TEST_IDEAS | 2,5,7 | `multiple` |
| `referrals/code_and_attribution.py.txt` | ADAPT | 5 | `apps/telegram_airdrop/referrals.py` |
| `referrals/regression_tests.py.txt` | REUSE_TEST_IDEAS | 5 | `apps/telegram_airdrop/tests_phase3_referrals.py` |
| `rankings/redis_projection.py.txt` | ADAPT | 9 | `apps/telegram_airdrop/leaderboard.py` |
| `rankings/safe_rebuild.py.txt` | REUSE_ADAPT | 9,11 | `apps/telegram_airdrop/leaderboard.py` |
| `rankings/regression_tests.py.txt` | REUSE_TEST_IDEAS | 9,11 | `apps/telegram_airdrop/tests_leaderboard.py` |
| `transactions/atomic_mutation_patterns.py.txt` | ADAPT | 3,4,8 | `apps/telegram_airdrop/api/views.py` |
| `transactions/regression_tests.py.txt` | REUSE_TEST_IDEAS | 3,4,8,11 | `multiple` |
| `migrations/safe_backfill.py.txt` | ADAPT | 3,5,11 | `apps/telegram_airdrop/migrations/0004_backfill_referral_codes.py` |
| `migrations/numeric_conversion.py.txt` | ADAPT | 3,8,11 | `apps/telegram_airdrop/migrations/0005_phase4a_integer_scoring.py` |
| `notifications/in_app_notifications.py.txt` | ADAPT | 6,10 | `multiple` |
| `broadcast/admin_broadcast_core.py.txt` | ADAPT | 10 | `apps/telegram_airdrop/tasks.py` |
| `broadcast/album_state_atomicity.py.txt` | ADAPT | 10 | `services/fastapi_webhook/main.py` |
| `broadcast/regression_tests.py.txt` | REUSE_TEST_IDEAS | 10 | `apps/telegram_airdrop/tests.py` |

## Selection principle

Only mechanisms that can materially save implementation/review effort or encode a previously discovered failure case are retained. UI/template-specific code, exchange/account code, obsolete economic rules, and generic boilerplate were intentionally omitted.
