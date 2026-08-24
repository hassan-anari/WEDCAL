# Phase reading map

This is a default map. The phase prompt supplied by the reviewer is authoritative and may narrow it further.

## Phase 2 — Telegram Identity & Ingestion (already implemented)
Read only for future regression comparison if needed:
- `telegram/miniapp_auth_core.py.txt`
- `telegram/webhook_security_start.py.txt`
- `telegram/telegram_api_client.py.txt`
- `telegram/regression_tests.py.txt`

## Phase 3 — Season, Progression, Config & Rewards
Useful:
- `transactions/atomic_mutation_patterns.py.txt`
- `transactions/regression_tests.py.txt`
- `migrations/safe_backfill.py.txt`
- `migrations/numeric_conversion.py.txt`
Do **not** reuse the old season reset/economy rules.

## Phase 4 — Daily, Missions & Typing
Useful only for transactional/idempotency mechanics:
- `transactions/atomic_mutation_patterns.py.txt`
- `transactions/regression_tests.py.txt`
No legacy implementation exists for the current mission strategy framework or Typing wallet.

## Phase 5 — Network (Referral + Creator/Group)
Useful:
- `referrals/code_and_attribution.py.txt`
- `referrals/regression_tests.py.txt`
- `telegram/telegram_api_client.py.txt`
- `telegram/regression_tests.py.txt` (group/start boundary cases only)
Do not reuse old activation thresholds, reward percentages, or direct group-message scoring.

## Phase 6 — Profile & Collection
Useful:
- `notifications/in_app_notifications.py.txt` only if profile notification summaries are involved.
No reusable legacy collection/achievement implementation exists.

## Phase 7 — Clan Core / Governance / Moderation / Recovery
Useful:
- `telegram/telegram_api_client.py.txt`
- `telegram/regression_tests.py.txt` for Telegram boundary/error cases
No legacy Clan governance implementation is approved.

## Phase 8 — Clan Progression & Economy
Useful:
- `transactions/atomic_mutation_patterns.py.txt`
- `transactions/regression_tests.py.txt`
No old group reward economics should be ported.

## Phase 9 — Arena Rankings
Primary reusable asset:
- `rankings/redis_projection.py.txt`
- `rankings/safe_rebuild.py.txt`
- `rankings/regression_tests.py.txt`

## Phase 10 — Notifications / Broadcast / Admin
Useful:
- `notifications/in_app_notifications.py.txt`
- `telegram/telegram_api_client.py.txt`
- `broadcast/admin_broadcast_core.py.txt`
- `broadcast/album_state_atomicity.py.txt`
- `broadcast/regression_tests.py.txt`

## Phase 11 — Season Close & Hardening
Useful for failure-mode ideas, not old business semantics:
- `transactions/regression_tests.py.txt`
- `migrations/safe_backfill.py.txt`
- `rankings/regression_tests.py.txt`
- `telegram/telegram_api_client.py.txt`
