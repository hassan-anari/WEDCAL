# Private Telegram economic alerts

Economic monitoring detects durable operational failures independently of economic authority.
Notifications go only to database-registered administrators in their private Telegram chats.
There is no economic email transport or fallback. Unrelated Django email functionality is unchanged.
All notification text, error codes, command output, documentation and synthetic evidence are English.

## Detection and incident history

Existing detectors and stable fingerprints `(code, Season, Epoch)` remain unchanged:

| Code | Meaning |
| --- | --- |
| EPOCH_FREEZE_OVERDUE | Mutable Epoch beyond cutoff/settlement gate plus configured grace |
| DLX_SETTLEMENT_OVERDUE | Frozen/settling Epoch beyond its permitted deadline |
| DLX_LIFECYCLE_BLOCKED | Latest compatible lifecycle attempt blocked or failed |
| GROWTH_FINALIZATION_FAILED | Configured Growth finalization failed before Freeze |
| ACCOUNTING_RECONCILIATION_MISMATCH | Canonical reconciliation failed or category set incomplete |
| ECONOMIC_WORKER_STALE | Running durable attempt exceeded its configured age |
| ECONOMIC_WORKER_REPEATED_FAILURE | Failures exceeded the configured count/window |
| SEASON_CLOSE_BLOCKED | Failed Close or durable manual blocker |

Pause and settlement-gate handling are preserved. A failed scan never resolves existing incidents.
OPENED, REOPENED, SEVERITY_CHANGED and RESOLVED create immutable events. Their severity is pinned
from the configured rule at that transition; notifications read that durable event, never current
logs. Monitoring and delivery have independent default-off switches:
`ECONOMIC_ALERTS_ENABLED=false` and `ECONOMIC_ALERT_DELIVERY_ENABLED=false`.

## Exact recipient authorization

`apps.arena.alert_delivery.authorized_operators()` implements:

```python
permission = Permission.objects.filter(
    content_type__app_label="arena",
    content_type__model="economicalert",
    codename="receive_economic_alert",
)
BotAdminOperator.objects.filter(
    enabled=True,
    telegram_user_id__gt=0,
    user__is_active=True,
    user__is_staff=True,
).filter(
    Q(user__is_superuser=True)
    | Q(user__user_permissions__in=permission)
    | Q(user__groups__permissions__in=permission)
).distinct()
```

The exact receive permission is `arena.receive_economic_alert`. Django superusers have the normal
implicit permission, but must still have an enabled operator mapping and active staff identity.
The query also annotates an `Exists` against the existing Player with matching `telegram_user_id`
and non-null `first_private_contact_at`. No Player is created. A missing private contact creates a
DEAD per-operator delivery with `PRIVATE_CONTACT_REQUIRED`; no API call is made. A positive
Telegram user ID is used as its private chat ID; group/channel IDs are never accepted.

There is no recipient environment list. `TELEGRAM_ADMIN_IDS` and
`notifications.manage_broadcasts` govern existing broadcast controls and do not confer economic
alert authority. Existing operator enrollment remains unchanged. Assign receive permission via
the normal restricted Django user/group permission workflow; this PR assigns no live permissions,
creates no live operators and does not activate delivery. A staff economic auditor requires this
explicit receive permission to obtain messages. Read-only Admin inspection continues to require
its existing economic view/auditor permissions separately.

Recipients are discovered when an event is materialized. Before every external send, a fresh
query revalidates enabled state, staff/active status, permission, matching pinned Telegram identity
and known private contact. Revocation after queueing causes DEAD / RECIPIENT_AUTHORITY_REVOKED.
As with any remote API, a permission change after the final check cannot recall a request already
in flight. New recipients receive subsequent events; no historical mass backfill runs automatically.
If there are no authorized operators, an immutable ROUTING_FAILED event records
NO_AUTHORIZED_OPERATOR. No fallback destination is invented. Missing/unreachable routes, DEAD
and UNCERTAIN results are visible through Admin and `operational_snapshot.economic_alerts`.
Use external application/Beat liveness monitoring as well: a stopped scanner cannot alert on itself.

## Transaction and retry architecture

1. Scanner holds its existing PostgreSQL advisory transaction lock; creates incident transitions
   and unique `(event, operator)` and `(event, private chat)` deliveries, with immutable operator/channel/private-ID snapshots.
2. A separate scheduled worker locks one due PENDING delivery with `skip_locked`, checks earlier
   PENDING/SENDING/UNCERTAIN events for the same incident/operator, persists a UUID lease and
   DISPATCH_STARTED audit event, and **commits**.
3. It revalidates authority, then uses `apps.telegram.bot_api.send_message` outside every database
   transaction. The existing client applies the shared bot-wide send/method/chat admission budgets,
   Redis outage fail-closed behavior and Telegram 429 global pause. No broadcast/Notification/
   campaign or gameplay record is created.
4. A short final transaction compares the lease token and persists the result and immutable
   DELIVERY_RESULT. Alert failures cannot roll back or block economic workers; alert scanning and
   dispatch are separate Celery tasks and do not run in Freeze/Growth/Settlement/Close transactions.
5. Scheduled scans redispatch bounded due work after broker restarts. Duplicate tasks cannot claim
   an active lease or resend a completed record. Expired SENDING leases become UNCERTAIN, never
   PENDING. This also handles death before the external call conservatively.

| Outcome | Durable result and recovery |
| --- | --- |
| Valid successful response with positive message ID | DELIVERED; Telegram acceptance, not proof of a human reading |
| 429 / local rate limit / budget unavailable | Bounded PENDING retry; exponential backoff, never earlier than retry_after |
| Retry count exhausted | DEAD; permissioned explicit retry after repair |
| Forbidden / chat not found | DEAD with safe code; operator must start/unblock the bot privately before retry |
| Missing bot configuration / revoked administrator | DEAD; repair configuration/authority through existing workflows |
| Ambiguous timeout, connection loss, 5xx, malformed success or unexpected client exception | UNCERTAIN; no automatic retry |
| Worker dies with active send claim | Lease expiration becomes UNCERTAIN; no blind replay |
| Other explicit non-ambiguous rejection | DEAD / TRANSPORT_REJECTED |

Telegram has no alert idempotency key. Exactly-once external delivery is not claimed. If a request
was accepted but its acknowledgement/database commit was lost, an operator must investigate the
private chat and worker history. Stop/terminate the old worker and ensure no request remains in
flight before reconciliation. Absence of a visible message alone is not proof it was never sent.
Prefer ABANDON if the result cannot be established. The three explicit review decisions are
CONFIRM_DELIVERED (positive message ID required), CONFIRM_NOT_SENT_AND_RETRY (requires documented
positive evidence that sending did not occur), and ABANDON. A misleading human decision can cause
a duplicate; every decision preserves actor/reference digest and original attempts/history.
Uncertain opening events block later recovery messages until reviewed. DEAD events are terminal
and do not indefinitely block recovery. Operator identity changes cannot retarget a historical delivery, and the same private chat cannot
receive a duplicate event under a replacement operator mapping. Manual retries retain the pinned destination and do not
reset attempt counts. The recipient is always reauthorized again at send time.

Operational bounds remain configurable: scan interval, batch size, failure count/window,
max attempts, retry base/max and delivery lease seconds. Rule overrides control enabled/severity
and supported deadline grace. Shared Telegram timeout/rate budgets use the existing Bot API client;
there is no separate economic recipient list, token, SMTP configuration or transport timeout.

## Safe operator commands

The management shell is restricted operational access. Actor accounts must be active staff;
mutations/recovery require `arena.control_economic_automation`. Commands default to dry-run.
References are required and hashed into durable audit provenance. IDs below are placeholders.

```bash
# Read-only detection and authorized routing preview; never sends.
python manage.py monitor_economics

# Persist monitoring transitions only; scheduled delivery is still independently gated.
python manage.py monitor_economics --apply

# Preview a private test using a durable event and authorized operator.
python manage.py monitor_economics --test-event EVENT_ID --actor OPERATOR --reference TICKET

# Prepare a durable HELD test; no scheduled worker can send HELD records.
python manage.py monitor_economics --test-event EVENT_ID --actor OPERATOR --reference TICKET --apply

# Only after separate owner authorization for a real test; NOT executed in this PR.
python manage.py monitor_economics --test-event EVENT_ID --actor OPERATOR --reference TICKET --apply --send-test

# Preview repair, then append --apply after reviewing the exact record.
python manage.py monitor_economics --retry-delivery DELIVERY_ID --actor OPERATOR --reference TICKET
python manage.py monitor_economics --resolve-delivery DELIVERY_ID --resolution CONFIRM_DELIVERED --message-id MESSAGE_ID --actor OPERATOR --reference TICKET
python manage.py monitor_economics --resolve-delivery DELIVERY_ID --resolution CONFIRM_NOT_SENT_AND_RETRY --actor OPERATOR --reference TICKET
python manage.py monitor_economics --resolve-delivery DELIVERY_ID --resolution ABANDON --actor OPERATOR --reference TICKET

# Preview routing again after an operator is enrolled/authorized; append --apply to materialize.
python manage.py monitor_economics --route-event EVENT_ID --actor OPERATOR --reference TICKET
```

Tests additionally require `arena.test_economic_alert`, receive permission, an enabled operator
mapping belonging to the actor and an existing private contact. Tests target **only that actor**,
not all previewed recipients. The report distinguishes the intended test operator from normal
routing recipients. Repeating the same source-event/actor/reference reuses the same held/test
record. Only `--apply --send-test` releases it, with an immutable TEST_AUTHORIZED event; scheduled
workers never release HELD tests. A repaired test remains HELD until explicitly authorized again.
The delivery switch must be enabled separately for any real send. Actual test release obeys event
ordering barriers and can therefore remain pending behind earlier unresolved notifications.

Messages are plain English text without parse_mode or user-entered labels/evidence. Internal
Season IDs avoid disclosing names or introducing non-English/untrusted content. Each includes
severity, event, transition, Season/Epoch, event-time status, incident/event IDs and explicit UTC
time plus the Admin action. It never includes tokens, private messages, source evidence or amounts.

## Migration and activation prerequisites

Migration 0031 is additive; migrations 0028–0030 are unchanged. Existing route snapshots, delivered
states, timestamps and events remain intact as LEGACY history. Pending legacy work is retired to
DEAD / LEGACY_TRANSPORT_DISABLED with an immutable CHANNEL_RETIRED event recording prior status.
No legacy record can be queued/re-enabled, and no email fallback exists. PostgreSQL guards pin
new operator identities, require positive private targets, prevent duplicate event/operator rows
and prohibit deleting delivery history. Reverse migration refuses before any DDL once private
Telegram history exists; empty disposable reverse/forward remains supported.

Before an independently authorized rollout: pause old alert scanning/sending and drain/terminate
old transport workers, back up normally, apply migrations, verify existing operator mapping and
private contact, assign receive/test permissions through controlled Django IAM, configure the
existing bot/shared budgets and workers, review dry-runs, and separately authorize a real private
test and default-off switches. Do not run old application workers against the migrated schema.
No live delivery, permissions, operator enrollment or real test was performed here.

## Verification

Real PostgreSQL tests exercise detection/recovery, exact permission population, direct/group
permissions, multiple recipients, no recipients/contact, revocation, private-only IDs, English
messages, 429 retry_after and shared budget admission, forbidden/chat-not-found, ambiguous sends,
lease/crash recovery, concurrent dispatch, deduplication, DEAD/UNCERTAIN review, held tests, Admin
redaction and immutable guards. Actual shared HTTP client paths use mocked HTTP responses or
budget denial; no Telegram endpoint is contacted. Real legacy migration and populated-reverse
refusal tests preserve historical snapshots. Final counts/SHA/CI links are recorded on Draft PR #38.
