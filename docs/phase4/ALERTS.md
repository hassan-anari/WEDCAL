# Economic operational alerts

Monitoring is implemented in `apps/arena/alerts.py`, scheduled through the existing Celery Beat
infrastructure and exposed in `operational_snapshot.economic_alerts`. Delivery uses Django's email
transport rather than the player-facing Telegram notification/broadcast system. **Both detection and
delivery default off.** No destination, credential, live delivery or economic authority is seeded.

| Condition | Canonical evidence | Default severity / operational threshold |
| --- | --- | --- |
| EPOCH_FREEZE_OVERDUE | OPEN/DRAINING Epoch after expected cutoff/gate | ERROR / 900 seconds |
| DLX_SETTLEMENT_OVERDUE | FROZEN/SETTLING Epoch after expected cutoff/gate | ERROR / 900 seconds |
| DLX_LIFECYCLE_BLOCKED | latest authorized attempt BLOCKED/FAILED | ERROR |
| GROWTH_FINALIZATION_FAILED | configured Growth and failed Growth/Freeze stage | CRITICAL |
| ACCOUNTING_RECONCILIATION_MISMATCH | failed reconciliation stage or canonical settled-category check | CRITICAL |
| ECONOMIC_WORKER_STALE | durable RUNNING attempt beyond configured age | ERROR / 3,900 seconds |
| ECONOMIC_WORKER_REPEATED_FAILURE | recent FAILED/INTERRUPTED attempts and no successful latest attempt | CRITICAL / 3 in 3,600 seconds |
| SEASON_CLOSE_BLOCKED | durable FAILED Close or MANUAL blocker | CRITICAL |

Defaults are runtime operational inputs, not economic formulas. `ECONOMIC_ALERT_RULE_OVERRIDES`
configures per-code enabled/severity and deadline/stale grace values; nonzero grace is rejected for
event rules. Expected deadlines use `max(epoch.ends_at, configured settlement gate time)` so the
Official gate does not generate false overdue alerts. A deliberately paused automation control
suppresses workflow-deadline/blocked alerts, while actual accounting mismatches and stale RUNNING
attempts remain visible. Alert resolution means the condition is no longer detected; it does not
approve a ledger, claim a successful settlement or erase the original failure.

## State and delivery

A stable digest of code/Season/Epoch identifies an incident. One PostgreSQL advisory transaction lock
serializes scans; repeated unchanged findings update the projection without duplicating events.
OPENED, REOPENED, SEVERITY_CHANGED and RESOLVED append immutable events and per-route outbox rows
atomically. A failed scan or invalid route configuration cannot resolve existing alerts. Evidence
contains scoped internal IDs, stages, timestamps, counts and reconciliation outcomes, not raw message
content, recipient lists, secret credentials or unfiltered exception strings.

Each event's routing snapshot is pinned and its per-route identity is unique. Delivery workers use
`select_for_update(skip_locked=True)` to avoid concurrent sends of the same pending row. Transport
errors persist a generic error, attempt count, capped exponential backoff and eventually DEAD status.
The Beat scanner recovers pending outbox work after application/broker restarts. Duplicate queued
tasks do not resend a completed delivery.
Pending deliveries on the same incident/route are sent in event order, so recovery cannot overtake
an opening that is still pending. A DEAD predecessor does not block all future recovery messages.
SMTP has an unavoidable acknowledgement gap: a process
crash after the server accepted mail but before DB commit may cause a duplicate. Delivery is
**at least once**, with a stable Message-ID to assist downstream deduplication. This is not a monetary
exactly-once guarantee and never changes economic allocations.
DELIVERED means the configured email backend accepted the message, not that a human read it or
that every downstream mail relay completed delivery. Live destination receipt testing remains an
explicit activation prerequisite.

Detection and delivery are independently controlled by ECONOMIC_ALERTS_ENABLED and
ECONOMIC_ALERT_DELIVERY_ENABLED. Disable delivery to pause external messages, including queued work.
An already executing bounded send may finish. Turning off monitoring alone stops new detection.

## Configuration and activation prerequisites

Runtime routes contain named destinations with `recipients`, `from_email` and a severity list.
There are no hardcoded recipients or credentials. Public test-only example:

```json
{"disposable-ops":{"recipients":["operator@example.invalid"],"from_email":"alerts@example.invalid","severities":["WARNING","ERROR","CRITICAL"]}}
```

Set this JSON in ECONOMIC_ALERT_ROUTES. Explicitly select ECONOMIC_ALERT_EMAIL_BACKEND:
`django.core.mail.backends.locmem.EmailBackend` captures disposable tests; live SMTP requires
`django.core.mail.backends.smtp.EmailBackend` and owner-authorized EMAIL_HOST/PORT/HOST_USER/
HOST_PASSWORD/USE_TLS. Credentials remain runtime secrets and never enter an event or route snapshot.
The backend setting is empty by default; enabled delivery refuses an unknown/implicit backend.

Other controls: ECONOMIC_ALERT_SCAN_SECONDS, BATCH_SIZE, FAILURE_COUNT, FAILURE_WINDOW_SECONDS,
MAX_ATTEMPTS, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS and SEND_TIMEOUT_SECONDS, all prefixed
ECONOMIC_ALERT_. `.env.example` lists operational defaults. Route validation, sender/recipient
validation and positive workload bounds fail closed. Metrics report OPEN incidents and delivery
status counts, including DEAD. Configure external monitoring of that dashboard/log stream separately.

```sh
# Safe read-only detection and routing validation; sends nothing.
python manage.py monitor_economics

# Persist findings only, when monitoring is explicitly enabled; sends nothing itself.
python manage.py monitor_economics --apply

# After diagnosing a DEAD delivery, an authorized economic operator can preview one retry.
python manage.py monitor_economics --retry-delivery DELIVERY_ID --actor OPERATOR --reference INCIDENT
# --apply explicitly requeues it; the delivery switch still controls actual sending.
```

Manual retry requires active staff with `arena.control_economic_automation`, records an immutable
RETRY_REQUESTED event and preserves total attempt counts and the original routing snapshot. Ordinary
audit viewers cannot retry through Admin. Correct or revoke routing safely with delivery paused;
inspect pinned pending destinations before reenabling delivery.

No real destination or external message was configured/sent for this PR. Before live use, the owner
must authorize destinations/credentials, assign operator permissions, validate receipt/recovery in
an approved environment, and explicitly enable monitoring/delivery. These steps do not enable DLX.
If the application or Beat is entirely down, it cannot report its own outage; an external liveness
monitor for the existing health endpoints/Beat is still a deployment prerequisite.

PostgreSQL tests cover each condition, gate/pause behavior, configurable threshold/severity, durable
deduplication/recovery, invalid-scan safety, transport retries/exhaustion/manual recovery, pinned
routing, concurrent scans/delivery and read-only/redacted Admin. All sends use the locmem backend;
`inspection/alerts/` and CI artifacts contain synthetic captured delivery evidence.
