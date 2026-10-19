# Economic alert delivery replacement review

Reviewed starting point: `cdd0aa372889956868708ba2c02bd29db5c1e1b7`, existing Draft PR #38.
This is a delivery-channel correction inside the full Phase 4 scope. It does not authorize
Production delivery, operator enrollment, permission assignment, economic activation or deployment.
Final commit SHA and all CI links/results are recorded on that same Draft PR.

## Removed economic components

| File | Removed component | Replacement |
| --- | --- | --- |
| apps/arena/alerts.py | EmailMessage/get_connection/validate_email imports, address-route validation, message creation and in-transaction send/retry implementation | Incident detection preserved; separate private Telegram outbox service |
| dexo_land/settings/base.py | ECONOMIC_ALERT_EMAIL_BACKEND, ECONOMIC_ALERT_ROUTES, ECONOMIC_ALERT_SEND_TIMEOUT_SECONDS | Database-authorized operators; shared Bot API client; configurable delivery lease |
| dexo_land/settings/base.py | EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, EMAIL_USE_TLS environment bindings introduced solely for economic SMTP | Removed economic-only SMTP wiring; Django's unrelated/default email support is unchanged |
| .env.example | The corresponding backend/address-route/SMTP/timeout examples | Independent default-off switches and delivery lease setting; no recipient IDs or new credentials |
| apps/arena/tests/phase3/test_economic_alerts.py | Mail outbox/address assertions and transport fixtures | Mocked private Telegram calls and English durable-event assertions |
| docs/phase4/ALERTS.md and related matrix/inspection reports | Address routing, SMTP instructions, previous channel activation/examples | English private Telegram operator runbook, mocked reports and safe uncertain recovery |
| docs/phase4/numeric-inventory.csv | References to removed transport code | Regenerated repository numeric inventory |

Applied migrations 0028–0030 are unchanged. Their original JSON route snapshots remain immutable
historical data; no executable legacy transport survives. Migration 0031 adds private recipient and
lease fields, retires pending legacy rows with explicit audit events, and prevents requeue. It does
not rewrite monetary history or erase previous delivery attempts/events. No fallback exists.

## Authorization and delivery

See [the exact query, permission model and operational runbook](ALERTS.md).
Only enabled `BotAdminOperator` mappings for active staff with `arena.receive_economic_alert`
(direct/group permission or normal superuser authority) qualify. Positive private chat IDs and
existing private contact evidence are required. Broadcast permission/environment allowlists are
independent. Recipient identity/authority is freshly checked immediately before dispatch.

Unique per-event/operator rows pin routing provenance. Short claim/result transactions surround
an external Bot API call made outside database transactions. Shared Telegram budgets and 429
pauses are reused. Explicit safe rejections retry within configured bounds; forbidden/unreachable
or revoked destinations become DEAD. Ambiguous sends and expired claims become UNCERTAIN.
No automatic uncertain retry exists. Permissioned review records actor, decision, reference digest
and outcome. Prepared test messages stay HELD until separately authorized for sending to the
requesting operator's own private chat. Real tests were not run.

## Evidence and limits

- 46 targeted PostgreSQL detection/private-transport tests passed in 118.19 seconds on a fresh isolated database.
- Empty isolated rollback/forward cycle passed in 30.93 seconds.
- Real schema backward-to-0030/forward-to-0031 test preserved old route snapshots, previously
  delivered status/timestamps/attempts and incident events, while retiring only pending legacy work.
- Real populated private-history reverse refused before DDL; original history-protection test remains.
- The broader policy/configuration/Growth/Admin/alert subset passed 101 tests in 126.31 seconds.
- Existing concurrent scan/delivery, correction/Admin and economic lifecycle assertions remain.
- [Synthetic opening/recovery report](inspection/alerts/lifecycle.json) captures only mocked sends.
- The final broader functional subset and complete CI counts are reported on Draft PR #38.

No changed economic policy/configuration, Growth, Freeze, allocation, correction or Close service
is part of this channel correction. Tests verify no Player auto-creation, campaign/broadcast or
notification records and no DLX allocation from sending. Existing full lifecycle/conservation
regressions run again in CI. No large-scale benchmark phase is started.

Before real activation, independently review the PR and then separately authorize rollout,
configure existing bot access/shared budgets, verify database operator enrollment/permissions and
private contact, pause old workers before migrating, review previews, authorize a real private test
and enable the independent switches. Delivery acceptance does not prove a recipient read a message.
A blocked bot, stale contact evidence or stopped scanner requires external operational monitoring.
Telegram cannot guarantee exactly-once external sends: unresolved acknowledgement gaps remain
UNCERTAIN until evidence-backed human reconciliation, with ABANDON as the conservative choice.
Large realistic performance/capacity certification remains explicitly deferred by the owner.
