# B2 Tap v2 — backend wire contract 2.0

C-01/P-02/P-03 are coordinated B2 workstreams. Frontend main inspected through the GitHub connector at FROCAL `2e8666a115f04a9633945d267e3b11a1d4d89edd`: legacy `/base/taps`, five-second batching, local daily counters and cooldown need replacement by this contract. Frontend design is unchanged; frontend publication belongs to its paired agent.

## Routes

All economic routes use B1 `Authorization: tma <verified Telegram initData>`. Identity/XP/Level/balances/cooldown never come from mutations. Extra JSON fields are rejected. Session UUID is persisted with the account/Season-scoped outbox and is not authentication.

- `GET /api/v1/base/v2/tap-state?session_id=<UUID>`: authoritative snapshot; omit session for state without a redeemable lease. Only the matching session receives active lease details.
- `POST /api/v1/base/v2/tap-leases`: `{"session_id":"UUID","takeover":false,"expected_generation":null}`. Same session recovers a lost issuance response. Explicit takeover needs current expected generation.
- `POST /api/v1/base/v2/tap-batches`: `{"lease_id":"UUID","session_id":"UUID","generation":1,"sequence_start":1,"tap_count":25,"idempotency_key":"stable-safe-key"}`. Positive strict integer count/sequence; key max 80 `[A-Za-z0-9._:-]`. Range is inclusive `[sequence_start, sequence_start + tap_count - 1]`. Genuine concurrent touches occupy separate consecutive intents within a batch; no finger-event database transactions.

Executable schemas: [schemas.py](../apps/base_tap/schemas.py), [OpenAPI](fixtures/tap-v2-openapi.json), [fixtures](fixtures/tap-v2.json).

Batch success returns `duplicate`, original `accepted_taps/rejected_taps/earned_points`, immutable `receipt_id`, original `sequence_through`, and a **fresh** `snapshot`. Snapshot includes player/Season/config/server time, issuance/gameplay flags, generation/revision/receipt_cursor, remaining burst, cooldown, active lease, committed progression and Season Points, current Level/Dexo threshold policy. Lease binds player via authenticated ownership, opaque IDs, generation, Season/config, authorized allowance, pinned reward/batch/interval, expiration and remaining sequence range. `authorized_allowance` is allowance when that lease was issued, not a new grant. `burst_remaining` is the unconsumed shared allowance.

## Semantics

| Situation | Result |
|---|---|
| First burst | Initially 50 authorized taps; no daily cap/reset |
| 50th accepted tap | Starts initially 10s cooldown at server acceptance |
| 51st | Zero further credit until a new eligible burst |
| Partial final batch | Accepts remaining prefix; rejects excess tail; advances only through accepted sequence. Never transfer tail to a new lease as earned XP |
| Same key/body | Same receipt/counts, fresh snapshot, zero new economic writes, even after lease expiry/cleanup/moderation/Season rollover |
| Same key/different body | Terminal `IDEMPOTENCY_CONFLICT` |
| Older sequence/different key | Terminal `SEQUENCE_REPLAY`; no receipt/credit |
| Gap | `SEQUENCE_GAP`; no receipt/credit. Submit missing preceding batch then retry exact gap body |
| Concurrent batches | Player row and unique ledger key serialize credit; prefer one in-flight frontend batch |
| Two devices | Second gets `ACTIVE_SESSION`; no independent allowance |
| Takeover | Compare expected generation, revoke old lease, retain remaining sequence/allowance/config. Warn before invalidating unsent old-device intents |
| Expiry | Terminal for new credit under that lease; renew without resetting interrupted allowance. Already committed receipt replay remains available |
| Interrupted cooldown | PostgreSQL retains it across refresh/devices/UTC rollover |
| Config changes | In-flight burst and renewals stay pinned to its immutable reward/cooldown. Next completed burst uses new effective config. Current authoritative Level/Dexo thresholds are returned |
| Issuance rollback | New leases paused, valid old leases still redeemable |
| Suspension/ban | Fresh credit blocked; committed receipts survive; FLAGGED remains playable |
| Epoch edge | Leases clipped at epoch end. Trusted server acceptance time supplies facts; shared epoch row lock fences exclusive freeze |
| Season close/rollover | Economic-window/close barriers reject new old-Season XP; committed receipts survive. New Season does not inherit old lease |
| Failure/cancellation before commit | Allowance/ledger/facts/projections all roll back |
| Lost response after commit | Exact same body/key retrieves committed receipt |

Prior lease authorization does not mean unsent taps committed. Storage destruction, expiry or revocation before transmission can lose uncommitted intents. HTTP cannot prove a real finger. Persist intent before instant authorized display; stop new local credit when lease/outbox safety is uncertain. Do not promise zero-network-time durability. Initially 120s lease validity is configurable transport policy; renew before expiry. Never trust client times to backdate epoch credit.

Within the same player/Season, compare snapshot `(receipt_cursor,revision)` lexicographically; stale responses cannot overwrite newer balances/leases. Cursor includes every settled reward for this player; revision tracks Tap mutations. On account switching, clear visible state and use a separately scoped outbox. Read/success snapshots use the same Player lock as economic settlement.

## Stable errors

Envelope: `{code,detail,retryable,retry_after_ms,snapshot}`. Error snapshot is currently null: recover through tap-state. No internal exception/input/secret is echoed.

| HTTP | Codes | Action |
|---|---|---|
| 401 | AUTH_MISSING, AUTH_INVALID, AUTH_EXPIRED | Reopen/authenticate; retain pending body |
| 403 | PLAYER_RESTRICTED | Stop fresh credit |
| 404 | LEASE_UNKNOWN | Terminal; committed key replay still works after cleanup |
| 409 | ACTIVE_SESSION, GENERATION_STALE, LEASE_REVOKED, LEASE_EXPIRED, ALLOWANCE_EXHAUSTED, SEQUENCE_REPLAY, IDEMPOTENCY_CONFLICT, SEASON_CHANGED, SEASON_UNAVAILABLE, SEASON_FINALIZED, EPOCH_FINALIZED | Recover state; never automatically remint under a new key |
| 409 | COOLDOWN, SEQUENCE_GAP | Retry after cooldown or preceding sequence |
| 422 | REQUEST_INVALID, BATCH_TOO_LARGE | Fix request; zero new committed value |
| 429 | RATE_LIMITED | Honor Retry-After/retry_after_ms, jitter/backoff, same key |
| 503 | ISSUANCE_DISABLED, LEGACY_DRAIN_REQUIRED, AUTH_UNAVAILABLE, CONFIG_UNAVAILABLE, TEMPORARY_UNAVAILABLE | Backoff; stop display without valid lease |

IP middleware may reject before auth with shorter `{detail,code:"RATE_LIMITED"}` plus Retry-After; clients must accept it. Redis throttling is disposable ingress protection. Burst/cooldown/sequence correctness is PostgreSQL-owned.

## Config, growth and compatibility

New sample `season1.game-config.v2.json`: schema 3 / economy 3.0.0 / Tap protocol 2; initially burst 50, cooldown 10s, XP 1, lease 120s, batch max 50, accepted-batch interval 50ms, lease metadata retention 7d. Transport safety maxima: allowance/batch 1000, lease 300s, metadata retention 30d. These are not daily earning limits. Historical schema 2 / economy 2.0.0 sample and published rows remain unchanged/parseable. Changes require a new effective-dated immutable config. v1→v2 cutover remains 00:00 UTC; later v2 changes pin existing bursts safely.

Legacy `/api/v1/base/taps` remains active by default; v2 issuance default false. Activation needs owner-approved pause/drain and paired frontend cutover. `BASE_TAP_LEGACY_INGRESS_ENABLED=true` prevents v2 issuance. After pause, new v1 requests get 409 but retained existing receipts replay. Old daily finalization only handles old events; new batches settle synchronously.

Lease metadata cleanup is bounded after expiry+retention. V2 has **no extra receipt table**: append-only RewardLedger carries immutable request/receipt provenance indefinitely with economic history. Each accepted batch adds one ledger, one source fact and ranking event, plus projection/burst updates; rejected requests add none. Growth is bounded per burst by allowance, not per day (worst valid fragmentation: one rewarded row per accepted tap). Owner must review measured retention/capacity/inflation before activation.
