# Rejected legacy patterns

These are explicit historical anti-patterns. Agents must not revive them.

- Separate FastAPI webhook service simply because the old project used one.
- Redis as the durable source of truth for inbound events or economic state.
- Redis `KEYS` scans in production flows.
- Deleting activity buffers before durable PostgreSQL persistence.
- Direct mutable score counters as the authoritative economy.
- Old hourly claim product rule.
- Old `>50 messages` referral activation rule.
- Direct reward for ordinary group messages or bot/group-add events.
- Old referral percentages/tier economics.
- Old season close that resets mutable counters and increments archived totals; current Season/Lifetime points contract is different.
- Exchange account / whitelist coupling.
- Float-based economy calculations.
- Bot-ID mismatch detection followed by `pass`/continued mutation (the old webhook contains this bug).
- Non-atomic generic cache read/modify/write rate limiter from `apps/utils/rate_limiter.py` as a correctness mechanism.
- Broadcast cleanup via Redis `KEYS`; use bounded/indexed keys or SCAN/known keys if a cleanup mechanism is required.
- Legacy `TelegramProfile` mega-model coupling identity, game score, referral state, and activity counters.
