# Automatic economic alert delivery review

Starting reviewed implementation: `cdd0aa372889956868708ba2c02bd29db5c1e1b7`, existing Draft PR #38.
Final owner instructions require automatic default-on monitoring/delivery, every active database
admin and bounded automatic retries even for ambiguous sends. No activation, per-recipient setting
or additional alert-specific permission is required. Final SHA and CI evidence are on that PR.

## Removed economic email components

| File | Removed component | Final replacement |
| --- | --- | --- |
| apps/arena/alerts.py | EmailMessage/get_connection/validate_email imports, email-address routes, message creation and in-transaction external delivery | Existing incident detection; automatic private Telegram outbox |
| dexo_land/settings/base.py | ECONOMIC_ALERT_EMAIL_BACKEND, ECONOMIC_ALERT_ROUTES, ECONOMIC_ALERT_SEND_TIMEOUT_SECONDS | Existing DB admin registry and shared Bot API client |
| dexo_land/settings/base.py | Economic-only EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, EMAIL_USE_TLS bindings | No economic SMTP wiring; unrelated/default Django email remains |
| .env.example | Backend/address/SMTP/timeout examples and disabled defaults | Default-on monitoring/delivery; optional maintenance pause/work bounds only |
| apps/arena/tests/phase3/test_economic_alerts.py | Mail outbox/address fixtures and assertions | Mocked private Telegram calls and English durable-event assertions |
| docs/phase4 and numeric inventory | Email routing, SMTP activation instructions and old channel fixtures | English automatic all-admin operations, bounded uncertain retries and regenerated inventory |

Historical generic route JSON remains immutable audit data. No executable email transport or
fallback survives. Applied migrations 0028–0030 are unchanged; unmerged 0031 introduces private
recipient/lease fields and retires pending legacy work without erasing history. It introduces no
receive/test permissions. Migration 0032 adds durable routing retry and cumulative retry-cycle
accounting with history guards. No accounting, policy formula or ledger is modified.

## Exact recipient rule

```python
BotAdminOperator.objects.filter(enabled=True, user__is_active=True)
```

Private-contact evidence is annotated only to determine reachability. No staff, superuser,
economic-auditor, broadcast-manager or other role is required. There is no additional permission,
TELEGRAM_ADMIN_IDS/environment allowlist, subscription, opt-in or per-admin severity filter.
Five active registered admins create five per-admin records. Unreachable/invalid identities receive
explicit failed records; one failed Telegram delivery cannot stop any other administrator's work.
Every supported incident transition and diagnostic test uses this same population.

## Automatic delivery and recovery

Existing Beat invokes the monitor automatically. Normal settings default to true for detection
and delivery; false is an optional explicit maintenance pause. Scanner transitions and per-admin
outbox records are durable. Short PostgreSQL claim/result transactions surround a shared Bot API
call made outside database transactions. Telegram shared budgets and retry_after are respected.

Temporary/configuration failures retry with bounded backoff. Ambiguous sends and expired worker
leases record UNCERTAIN and retry automatically, without human review. Rare duplicate admin
messages are explicitly accepted by the owner; event/outbox identities remain unique and attempts,
uncertainty and automatic retries remain audited. Exhausted retries and permanent failures become
visible DEAD records. Empty registry and missing bot credentials produce structured ERROR codes
and operational snapshot health errors. A durable pending fan-out automatically recovers when
registered admins become available.

Normal operation needs only existing bot credentials, active database administrator registrations,
private bot contact and normal broker/worker/Beat operation. There is no extra alert activation or
recipient setup. Optional diagnostics and maintenance use the existing economic management-control
permission; those action permissions never filter notification recipients.

## Evidence and limits

The targeted PostgreSQL detection/Telegram suite passed **51 tests in 54.36 seconds**, including:

- Five varied/no-role admins, an empty environment allowlist and no added permissions: every
  OPENED/REOPENED/SEVERITY_CHANGED/RESOLVED event creates exactly five independent records.
- Automatic Beat dispatch to all five; one forbidden private chat leaves the others delivered.
- Default-on normal deployment settings; tests explicitly use paused or mocked transport.
- 429/shared-budget backoff, automatic bounded timeout/uncertain retries and worker-crash recovery.
- Broker restart, deduplication, independent failure states and durable empty-registry repair.
- Explicit visible credential/registry errors, no HTTP on missing credentials and no email fallback.
- Real historical schema migration preservation and populated-history reverse refusal.

The final isolated migration cycle, complete regression counts and required CI links are recorded
on Draft PR #38. [Runbook and exact operational behavior](ALERTS.md) and synthetic inspection
reports cover normal all-admin operation and optional diagnostics. No actual Telegram endpoint,
live operator record, permission assignment or Production deployment was used. Large realistic
capacity validation remains separately owner-deferred; no performance phase is started.
