# B3 Bot Admin / delivery security matrix

Implementation: `apps/notifications/bot_admin.py`, shared ingress/delivery services. All tests use
synthetic identities and prohibit/mock outbound calls. No live audience is materialized or sent.

| Boundary / attack | Expected proof | Test module |
|---|---|---|
| Wrong/missing webhook secret | 401 before persistence; no panel state | `test_b3_bot_admin.py::test_webhook_secret_is_required_before_persisted_admin_callback` |
| Persisted but unauthenticated provenance | No admin session/campaign/delivery | `test_every_message_requires_complete_authorization[unpersisted_auth]` |
| Numeric ID absent from environment allowlist | No access | `test_every_message_requires_complete_authorization[no_allowlist]` |
| Missing/revoked internal manage_broadcasts permission | No message/callback action | `[no_role]`, `test_callbacks_cannot_be_stolen_expired_or_replayed[revoked_role]` |
| Disabled mapping / inactive internal user | No access | `[disabled]`, `[inactive]` |
| Authorized admin in a Telegram group | No administrative state or response | `[group]` message/callback variants |
| Actor/chat mismatch | No access | `[mismatch]` message/callback variants |
| Bot sender / forwarded / anonymous sender_chat | No access | `[bot]`, `[forwarded]`, `[anonymous]` |
| Stolen token, different actor | No campaign mutation | `[other_actor]` |
| Expired session / token / unknown token | No campaign mutation | `[expired]`, `[unknown]`, restart-recovery test |
| Replayed confirmation, even different update ID | Exactly one queue audit | full workflow replay test |
| Webhook retry / repeated private start | One canonical Player and one welcome delivery | welcome deduplication test |
| Restart/reopen / abandon draft | Durable draft, no implicit send | restart-recovery test |
| Explicit confirmation | Exact body/audience frozen; state MATERIALIZING | full workflow test |
| Cancel unsent campaign | Skip pending/retry; preserve in-flight uncertainty | audience and existing concurrency tests |
| Default rollout switches false | No new welcome/new-audience queue | default-disabled test |
| Group observed / Creator registered only | Not broadcast-enrolled | audience consent test |
| Bot membership or send permission lost | Skip target, never fake Player | group permission test |
| Local throttle / Telegram 429 / Redis budget outage | RETRY_WAIT, correct delay, no permanent FAILED | direct notification + user/group retry tests |
| Ambiguous send / expired processing lease | UNCERTAIN, never automatic resend | existing services/concurrency + audience tests |
| Broadcast starvation | Sending sub-budget leaves capacity for membership/avatar methods | `apps/telegram/tests/test_b3_budget.py` |

Private role assignment is not a bot command. A superuser configures the internal mapping in
Django Admin and deployment operators configure numeric IDs. General reward/wallet/database
administration is not exposed in Telegram. Operator audits contain action/state/correlation,
not bot tokens or signed authentication payloads. Draft text remains in permissioned campaign
storage; webhook payload retention follows the existing configurable purge policy.
