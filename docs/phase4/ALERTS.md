# Automatic private Telegram economic alerts

Normal deployed applications run economic monitoring and Telegram delivery automatically through
existing Celery Beat and workers. No activation command, per-Season alert enablement, recipient
configuration or additional permission is required. All active database-registered administrators
receive every economic incident transition in private Telegram messages. There is no email
transport or fallback. No deployment, live registration change or real message was performed here.

## Recipient source and mandatory fan-out

The exact recipient query is the existing administrator registry, without role/subscription filters:

```python
BotAdminOperator.objects.filter(
    enabled=True,
    user__is_active=True,
).select_related("user").annotate(
    private_contact=Exists(
        Player.objects.filter(
            telegram_user_id=OuterRef("telegram_user_id"),
            first_private_contact_at__isnull=False,
        )
    )
).order_by("pk")
```

There is no staff, superuser, economic-auditor, broadcast-manager, additional permission,
`TELEGRAM_ADMIN_IDS`, environment allowlist, subscription, opt-in or per-admin severity filter.
An enabled BotAdminOperator linked to an active User defines an active registered administrator.
Unmapped Django users are not registered bot administrators. Existing enrollment remains unchanged;
no new receive/test permissions are introduced or assigned.

Private contact is a reachability annotation, not a recipient filter. Five active administrators
create five independent records, including failures. Missing private contact becomes
DEAD / PRIVATE_CONTACT_REQUIRED; invalid nonpositive identities become DEAD / INVALID_PRIVATE_TARGET.
They never cause Player creation, group/channel sends or omission from reporting. A valid positive
Telegram user ID is its private chat ID. Immediately before each send, a fresh query checks current
registry enabled/active state, pinned identity and private reachability. No role is checked.

Every OPENED, REOPENED, SEVERITY_CHANGED and RESOLVED event fans out to all current active admins.
Diagnostic test events use the same all-admin population. Per-event/operator and per-event/private-
chat uniqueness preserve deduplication; a failed admin does not block delivery to other admins.
Existing immutable recipient snapshots cannot be retargeted after identity changes. New admins
receive subsequent events; completed historical fan-outs are not mass-broadcast again.

## Automatic operation and visible errors

`apps.arena.tasks.monitor_economic_operations` is registered in CELERY_BEAT_SCHEDULE, using the
existing broker and workers. Both ECONOMIC_ALERTS_ENABLED and ECONOMIC_ALERT_DELIVERY_ENABLED
**default to true**, including the example environment. They are optional explicit maintenance
pause controls; no operator needs to set them for ordinary deployment. Test settings explicitly
pause external notification work unless a test opts into mocked transport.

Each Beat tick checks routing health, scans durable economic conditions, atomically persists
incident/event/per-admin outbox records, repairs due pending fan-outs, and dispatches due deliveries.
The scanner remains independent of economic activation. Actual expected deadlines still honor the
approved settlement gate and existing economic automation/pause state; alert operation does not
activate or alter economic authority.

Missing existing Bot API credentials logs ERROR / TELEGRAM_BOT_CREDENTIALS_MISSING. An empty active
registry logs ERROR / NO_ACTIVE_REGISTERED_ADMIN. Both appear in operational_snapshot.economic_alerts
routing_health. The scanner keeps running; no fallback or silent disablement exists. A missing
registry also creates immutable ROUTING_FAILED evidence and a durable PENDING EconomicAlertFanout.
Bounded batches retry that fan-out with capped backoff; once admins exist, the original event is
materialized and dispatched automatically. No manual repair command is required. Credentials missing
during a send produce an explicit CONFIGURATION_ERROR with bounded retries and eventual DEAD state.

The normal prerequisites are the application's existing Telegram bot credentials, active database
administrator registrations, their private bot contacts, migrations and normal Beat/worker/broker
operation. No extra alert recipient or permission configuration is needed. External application/Beat
liveness monitoring remains necessary because a stopped process cannot execute its own scanner.

## Detection and durable history

| Code | Condition |
| --- | --- |
| EPOCH_FREEZE_OVERDUE | Mutable Epoch past its permitted cutoff/gate plus configured grace |
| DLX_SETTLEMENT_OVERDUE | Frozen/settling Epoch past its permitted deadline |
| DLX_LIFECYCLE_BLOCKED | Latest lifecycle attempt blocked/failed |
| GROWTH_FINALIZATION_FAILED | Configured Growth finalization failed before Freeze |
| ACCOUNTING_RECONCILIATION_MISMATCH | Canonical accounting/category reconciliation failed |
| ECONOMIC_WORKER_STALE | Durable running worker exceeded configured age |
| ECONOMIC_WORKER_REPEATED_FAILURE | Failures exceeded configured count/window |
| SEASON_CLOSE_BLOCKED | Failed Close or durable manual blocker |

Stable incident fingerprints, rule severity, deduplicated immutable transitions and safe evidence
are preserved. A failed scan never resolves existing incidents. Alert tasks do not calculate or
mutate Growth, Freeze, Settlement, corrections, Close or DLX balances. Delivery failures cannot
roll back economic transactions.

## Delivery, automatic retries and accepted duplicates

1. A short PostgreSQL transaction claims one due PENDING or UNCERTAIN delivery with skip_locked,
   persists a UUID send lease, increments its cumulative attempt count and writes DISPATCH_STARTED.
2. After commit, fresh registry/private-target checks run. The existing Bot API client sends plain
   text outside database transactions, using shared bot-wide/method/chat budgets and 429 pauses.
3. A short result transaction compares the lease token, persists status/message ID and immutable
   DELIVERY_RESULT with a safe transport code. Exceptions never expose credentials or raw evidence.
4. Future Beat ticks redispatch due PENDING/UNCERTAIN work after broker/worker restarts. Completed
   deliveries are not automatically sent again. Uncertain retries write AUTOMATIC_RETRY audit events.
5. Expired SENDING leases become UNCERTAIN with backoff, or DEAD when their attempt budget is
   exhausted. They retry automatically without a human approving or deciding whether sending occurred.

| Outcome | Automatic behavior |
| --- | --- |
| Valid success with positive message ID | DELIVERED; accepted by Telegram, not proof a human read it |
| 429 / local budget refusal / unavailable shared budget | Bounded automatic retry; honor retry_after as a minimum |
| Temporary rejection / missing bot configuration | Bounded retry and clear error code, then DEAD if unresolved |
| Ambiguous timeout, connection loss, 5xx, malformed acknowledgement, unknown client exception | Record UNCERTAIN; automatically retry with backoff within the same bound |
| Worker dies with an active send lease | Record SEND_LEASE_EXPIRED; automatically retry within the bound |
| Forbidden / chat not found / disabled or changed admin identity | DEAD with explicit safe code; continue other admins |
| Retry limit reached | DEAD with final reason; visible in Admin and metrics |

Telegram has no economic-alert idempotency key. The owner explicitly accepts rare duplicate
administrative notifications in exchange for automatic recovery. An accepted send whose response or
result commit was lost can be repeated. Internal incident/outbox identity remains unique, cumulative
attempts and uncertainty are audited, and retries are bounded. No manual intervention is required
before ordinary or ambiguous retries. Earlier unresolved events block only later events for the
same incident/admin; retries eventually deliver or become terminal, while other admins progress.

Operational defaults are configurable work bounds, not recipient settings: scan 60 seconds, batch
100, max attempts 5, retry base 30 seconds, retry cap 3600 seconds and send lease 60 seconds. Server
retry_after is never capped below Telegram's instruction. No arbitrary amounts or economic formulas
are introduced. An explicit optional maintenance retry starts another bounded cycle while retaining
the cumulative attempt count and recording its actor/reference digest.

## Messages and inspection

Messages are concise plain English, with no parse_mode or untrusted/user-entered labels. They
contain severity, event name, transition, internal Season ID, Epoch number, event-time status,
incident/event IDs, explicit UTC timestamp and an action to inspect Django Admin. Internal IDs
avoid leaking owner-entered names, raw evidence or non-English text. No amount, access token,
credential, private message or source proof is included.

EconomicAlert, EconomicAlertEvent, EconomicAlertDelivery and EconomicAlertFanout remain read-only
in Admin. Recipient snapshots are redacted; safe codes, attempts, channel, message IDs and lease/
retry times support inspection. Operational snapshots report routing health, pending fan-outs and
per-state delivery counts. Existing audit-view permissions govern inspection independently of
mandatory receipt by every active registered admin.

## Optional diagnostic and maintenance commands

Normal operation requires none of these commands. Restricted management-shell actions use the
existing arena.control_economic_automation permission and an active staff actor. This controls
manual actions only, never receipt. Diagnostic preparation also identifies the requesting registered
admin; diagnostics fan out to every active registered admin. No new alert-specific permission exists.

```bash
# Read-only incident/routing preview.
python manage.py monitor_economics

# Preview an optional all-admin diagnostic using a durable source event.
python manage.py monitor_economics --test-event EVENT_ID --actor OPERATOR --reference TICKET

# Prepare HELD diagnostic records; workers cannot send them yet.
python manage.py monitor_economics --test-event EVENT_ID --actor OPERATOR --reference TICKET --apply

# Optional real diagnostic requires separate owner authorization; NOT run in this PR.
python manage.py monitor_economics --test-event EVENT_ID --actor OPERATOR --reference TICKET --apply --send-test

# Optional exhausted-delivery maintenance preview; append --apply after deliberate review.
python manage.py monitor_economics --retry-delivery DELIVERY_ID --actor OPERATOR --reference TICKET
```

Optional HELD diagnostics require explicit send authorization because they are operator-requested
messages. Ordinary incident deliveries and all automatic retries never enter HELD or require an
approval. Preparing the same source-event/actor/reference reuses its records. Automatic retries
preserve the original per-admin destination. Existing explicit review/abandon commands remain
available for maintenance; they are not prerequisites for automatic uncertainty recovery.

## Migration and verification

Applied migrations 0028–0030 remain unchanged. Unmerged 0031 introduces private delivery fields,
retains all legacy routing/delivered history, retires pending old-channel work with immutable audit
events and prevents old-channel requeue. No email fallback exists. It introduces no receive/test
permissions. Invalid admin IDs may be represented only as DEAD, never queued private/group sends.
0032 adds durable indexed fan-out recovery and retry-cycle accounting; routing provenance/deletion
and populated downgrade guards preserve history. Empty disposable rollback/forward stays supported.
Before a separately authorized rollout, stop old transport workers and apply migrations normally;
normal economic monitoring then runs automatically through existing Beat. This PR performs no rollout.

PostgreSQL tests cover the exact five-admin acceptance case with varied/no roles, every transition,
one failing admin with the others delivered, automatic Beat dispatch, default-on normal settings,
empty-registry/credential errors and recovery, 429/backoff, automatic uncertain/crash retries,
bounded exhaustion, broker restart, deduplication, private-only targets, English formatting,
read-only Admin and real migration/history guards. Every test uses mocked Telegram delivery or
mocked HTTP/budget responses. Final counts, exact SHA and CI evidence are recorded on Draft PR #38.
