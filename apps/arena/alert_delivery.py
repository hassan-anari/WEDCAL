"""Private, database-authorized economic Telegram outbox. Never changes economics."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.arena.models import EconomicAlertDelivery, EconomicAlertEvent, EconomicAlertFanout
from apps.arena.policy import digest
from apps.notifications.models import BotAdminOperator
from apps.players.models import Player
from apps.telegram import bot_api

logger = structlog.get_logger(__name__)
CHANNEL = "TELEGRAM_PRIVATE"
TRANSITIONS = {"OPENED", "REOPENED", "SEVERITY_CHANGED", "RESOLVED", "TEST_REQUESTED"}
TRANSPORT_CODES = {
    "timeout",
    "connection_error",
    "http_error",
    "non_json_response",
    "telegram_not_ok",
    "unexpected_transport_error",
    "rate_limited",
    "local_rate_limited",
    "budget_unavailable",
    "forbidden",
    "chat_not_found",
    "recipient_authority_revoked",
    "configuration_error",
}


def authorized_operators() -> list[BotAdminOperator]:
    """Every enabled database admin with an active User; no role or subscription filters."""
    contact = Player.objects.filter(
        telegram_user_id=OuterRef("telegram_user_id"), first_private_contact_at__isnull=False
    )
    candidates = (
        BotAdminOperator.objects.filter(enabled=True, user__is_active=True)
        .select_related("user")
        .annotate(private_contact=Exists(contact))
        .order_by("pk")
    )
    return list(candidates)


def route_health(*, report_errors: bool = False) -> dict[str, Any]:
    count = BotAdminOperator.objects.filter(enabled=True, user__is_active=True).count()
    errors = []
    if not settings.TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_CREDENTIALS_MISSING")
    if count == 0:
        errors.append("NO_ACTIVE_REGISTERED_ADMIN")
    if report_errors:
        for code in errors:
            logger.error("economic_alert_operational_error", error_code=code)
    return {
        "active_admins": count,
        "bot_configured": bool(settings.TELEGRAM_BOT_TOKEN),
        "error_codes": errors,
        "monitoring_enabled": settings.ECONOMIC_ALERTS_ENABLED,
        "delivery_enabled": settings.ECONOMIC_ALERT_DELIVERY_ENABLED,
    }


def _retry_at(now: datetime, attempts: int, retry_after: int = 0) -> datetime:
    backoff = min(
        settings.ECONOMIC_ALERT_RETRY_MAX_SECONDS,
        settings.ECONOMIC_ALERT_RETRY_BASE_SECONDS
        * 2 ** min(max(attempts - 1, 0), settings.ECONOMIC_ALERT_RETRY_MAX_SECONDS.bit_length()),
    )
    return now + timedelta(seconds=max(backoff, retry_after))


def recipient_preview() -> list[dict[str, Any]]:
    return [
        {"operator_id": op.pk, "reachable": op.telegram_user_id > 0 and bool(op.private_contact)}  # type: ignore[attr-defined]
        for op in authorized_operators()
    ]


def _audit(row: EconomicAlertDelivery, outcome: str, **evidence: Any) -> None:
    EconomicAlertEvent.objects.create(
        alert_id=row.event.alert_id,
        outcome=outcome,
        severity=row.event.severity,
        evidence={"delivery_id": row.pk, "source_event_id": row.event_id, **evidence},
    )


@transaction.atomic
def materialize_event(
    event: EconomicAlertEvent,
    *,
    now: datetime | None = None,
) -> list[int]:
    event = EconomicAlertEvent.objects.select_for_update().get(pk=event.pk)
    if event.outcome not in TRANSITIONS:
        raise ValidationError("ECONOMIC_ALERT_EVENT_NOT_DELIVERABLE")
    now = now or timezone.now()
    fanout, _ = EconomicAlertFanout.objects.get_or_create(
        event=event, defaults={"next_attempt_at": now}
    )
    operators = authorized_operators()
    if not operators:
        if fanout.attempts == 0:
            EconomicAlertEvent.objects.create(
                alert_id=event.alert_id,
                outcome="ROUTING_FAILED",
                severity=event.severity,
                evidence={
                    "source_event_id": event.pk,
                    "error_code": "NO_ACTIVE_REGISTERED_ADMIN",
                    "reason": "NO_ACTIVE_REGISTERED_ADMIN",
                },
            )
        fanout.attempts += 1
        fanout.status, fanout.last_error_code = "PENDING", "NO_ACTIVE_REGISTERED_ADMIN"
        fanout.next_attempt_at = _retry_at(now, fanout.attempts)
        fanout.save()
        return []
    ids = []
    for op in operators:
        # Operator identities can be corrected through existing enrollment controls. A historical
        # recipient must never be retargeted or receive the same event under a different mapping.
        existing = (
            EconomicAlertDelivery.objects.filter(event=event)
            .filter(Q(operator_id=op.pk) | Q(telegram_user_id=op.telegram_user_id))
            .order_by("pk")
            .first()
        )
        if existing is not None:
            ids.append(existing.pk)
            continue
        valid_private_id = op.telegram_user_id > 0
        reachable = valid_private_id and bool(op.private_contact)  # type: ignore[attr-defined]
        error_code = (
            ""
            if reachable
            else "PRIVATE_CONTACT_REQUIRED"
            if valid_private_id
            else "INVALID_PRIVATE_TARGET"
        )
        row, created = EconomicAlertDelivery.objects.get_or_create(
            event=event,
            operator=op,
            defaults={
                "channel": CHANNEL,
                "telegram_user_id": op.telegram_user_id,
                "route_key": f"operator:{op.pk}",
                "route_config": {
                    "channel": CHANNEL,
                    "operator_id": op.pk,
                    "telegram_user_id": op.telegram_user_id,
                },
                "next_attempt_at": now,
                "status": "HELD"
                if reachable and event.outcome == "TEST_REQUESTED"
                else "PENDING"
                if reachable
                else "DEAD",
                "last_error_code": error_code,
            },
        )
        if created and not reachable:
            _audit(row, "DELIVERY_RESULT", status="DEAD", error_code=error_code)
        ids.append(row.pk)
    if fanout.status != "COMPLETE":
        fanout.attempts += 1
        fanout.status, fanout.last_error_code, fanout.completed_at = "COMPLETE", "", now
        fanout.save(update_fields=("attempts", "status", "last_error_code", "completed_at"))
    return list(dict.fromkeys(ids))


def retry_pending_fanouts(*, now: datetime | None = None) -> int:
    now = now or timezone.now()
    ids = list(
        EconomicAlertFanout.objects.filter(status="PENDING", next_attempt_at__lte=now)
        .order_by("pk")
        .values_list("event_id", flat=True)[: settings.ECONOMIC_ALERT_BATCH_SIZE]
    )
    for event in EconomicAlertEvent.objects.filter(pk__in=ids).order_by("pk"):
        materialize_event(event, now=now)
    return len(ids)


def format_message(event: EconomicAlertEvent, *, preview_test: bool = False) -> str:
    """Plain text; no parse_mode, raw evidence, user text or arbitrary configured labels."""
    alert = event.alert
    epoch = alert.epoch
    epoch_number = str(epoch.epoch_number) if epoch is not None else "Not applicable"
    # Internal IDs avoid leaking owner-entered names, sensitive metadata or non-English content.
    outcome = "TEST_REQUESTED" if preview_test else event.outcome
    status = (
        "RESOLVED" if outcome == "RESOLVED" else "TEST" if outcome == "TEST_REQUESTED" else "OPEN"
    )
    event_id = "Assigned on preparation" if preview_test else str(event.pk)
    event_time = (
        "Assigned on preparation (UTC)"
        if preview_test
        else event.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    return (
        "Dexo Land — Economic Alert\n\n"
        f"Severity: {event.severity}\n"
        f"Event: {alert.code.replace('_', ' ').title()}\n"
        f"Transition: {outcome}\n"
        f"Season: {alert.season_id}\nEpoch: {epoch_number}\nStatus: {status}\n"
        f"Incident ID: {alert.pk}\nEvent ID: {event_id}\n"
        f"Time: {event_time}\n\n"
        "Action: Review the economic incident in Django Admin."
    )


def _authorized(row: EconomicAlertDelivery) -> bool:
    # Revalidate current registry membership and active state immediately before the private send.
    if row.telegram_user_id is None or row.telegram_user_id <= 0:
        return False
    return any(
        op.pk == row.operator_id
        and op.telegram_user_id == row.telegram_user_id
        and bool(op.private_contact)  # type: ignore[attr-defined]
        for op in authorized_operators()
    )


def recover_expired_deliveries(*, now: datetime | None = None) -> int:
    """Record crashed-send uncertainty and schedule bounded automatic retries."""
    now = now or timezone.now()
    count = 0
    with transaction.atomic():
        rows = (
            EconomicAlertDelivery.objects.select_for_update(skip_locked=True)
            .filter(channel=CHANNEL, status="SENDING", lease_expires_at__lte=now)
            .select_related("event")
            .order_by("pk")[: settings.ECONOMIC_ALERT_BATCH_SIZE]
        )
        for row in rows:
            row.status = (
                "UNCERTAIN"
                if row.attempts - row.retry_cycle_start < settings.ECONOMIC_ALERT_MAX_ATTEMPTS
                else "DEAD"
            )
            row.last_error_code = "SEND_LEASE_EXPIRED"
            row.next_attempt_at = _retry_at(now, row.attempts - row.retry_cycle_start)
            row.save(update_fields=("status", "last_error_code", "next_attempt_at"))
            _audit(row, "DELIVERY_RESULT", status=row.status, error_code=row.last_error_code)
            count += 1
    return count


def deliver_alert(delivery_id: int, *, now: datetime | None = None) -> bool:
    if not settings.ECONOMIC_ALERT_DELIVERY_ENABLED:
        return False
    if connection.in_atomic_block:
        raise RuntimeError("Telegram economic delivery must run outside database transactions")
    from apps.arena.alerts import alert_rules

    alert_rules()
    now = now or timezone.now()
    recover_expired_deliveries(now=now)
    with transaction.atomic():
        row = (
            EconomicAlertDelivery.objects.select_for_update(of=("self",), skip_locked=True)
            .select_related("event__alert__epoch")
            .filter(
                pk=delivery_id,
                channel=CHANNEL,
                status__in=["PENDING", "UNCERTAIN"],
                next_attempt_at__lte=now,
            )
            .first()
        )
        if row is None:
            return False
        if EconomicAlertDelivery.objects.filter(
            event__alert_id=row.event.alert_id,
            operator_id=row.operator_id,
            event_id__lt=row.event_id,
            status__in=["PENDING", "SENDING", "UNCERTAIN"],
        ).exists():
            return False
        if row.attempts - row.retry_cycle_start >= settings.ECONOMIC_ALERT_MAX_ATTEMPTS:
            row.status, row.last_error_code = "DEAD", "AUTOMATIC_RETRY_EXHAUSTED"
            row.save(update_fields=("status", "last_error_code"))
            _audit(row, "DELIVERY_RESULT", status=row.status, error_code=row.last_error_code)
            return False
        if row.status == "UNCERTAIN":
            _audit(row, "AUTOMATIC_RETRY", previous_status="UNCERTAIN", attempt=row.attempts + 1)
        row.status = "SENDING"
        row.lease_token = uuid.uuid4()
        row.lease_expires_at = now + timedelta(
            seconds=settings.ECONOMIC_ALERT_DELIVERY_LEASE_SECONDS
        )
        row.attempts += 1
        row.save(update_fields=("status", "lease_token", "lease_expires_at", "attempts"))
        _audit(row, "DISPATCH_STARTED", attempt=row.attempts)
    # Claim committed. Authority is checked afresh immediately before calling the shared client.
    expected = {
        "channel": CHANNEL,
        "operator_id": row.operator_id,
        "telegram_user_id": row.telegram_user_id,
    }
    if row.route_config != expected or not _authorized(row):
        result = bot_api.TelegramResult(False, category="recipient_authority_revoked")
    else:
        try:
            assert row.telegram_user_id is not None  # Positive private target is DB-enforced.
            result = bot_api.send_message(int(row.telegram_user_id), format_message(row.event))
        except Exception:
            # Unknown client exception may follow a successful external send. Never log its text.
            result = bot_api.TelegramResult(
                False, category="unexpected_transport_error", ambiguous=True
            )
    with transaction.atomic():
        locked = (
            EconomicAlertDelivery.objects.select_for_update().select_related("event").get(pk=row.pk)
        )
        if locked.status != "SENDING" or locked.lease_token != row.lease_token:
            return False
        message_id = (result.result or {}).get("message_id")
        if result.ok and type(message_id) is int and message_id > 0:
            locked.status, locked.last_error_code = "DELIVERED", ""
            locked.telegram_message_id, locked.delivered_at = message_id, timezone.now()
        elif result.ambiguous or result.ok:
            locked.status = (
                "UNCERTAIN"
                if locked.attempts - locked.retry_cycle_start < settings.ECONOMIC_ALERT_MAX_ATTEMPTS
                else "DEAD"
            )
            locked.last_error_code = (
                "AMBIGUOUS_SEND_OUTCOME"
                if locked.status == "UNCERTAIN"
                else "AMBIGUOUS_RETRY_EXHAUSTED"
            )
            locked.next_attempt_at = _retry_at(
                timezone.now(), locked.attempts - locked.retry_cycle_start, result.retry_after or 0
            )
        elif result.category in {
            "forbidden",
            "chat_not_found",
            "recipient_authority_revoked",
        }:
            locked.status, locked.last_error_code = "DEAD", str(result.category).upper()
        else:
            # Retry temporary/configuration failures within the same bounded outbox policy.
            safe_retry = result.category in {
                "rate_limited",
                "local_rate_limited",
                "budget_unavailable",
                "configuration_error",
                "http_error",
                "telegram_not_ok",
            }
            locked.status = (
                "PENDING"
                if safe_retry
                and locked.attempts - locked.retry_cycle_start
                < settings.ECONOMIC_ALERT_MAX_ATTEMPTS
                else "DEAD"
            )
            locked.last_error_code = (
                str(result.category).upper() if safe_retry else "TRANSPORT_REJECTED"
            )
            locked.next_attempt_at = _retry_at(
                timezone.now(), locked.attempts - locked.retry_cycle_start, result.retry_after or 0
            )
        locked.lease_token = locked.lease_expires_at = None
        locked.save()
        _audit(
            locked,
            "DELIVERY_RESULT",
            status=locked.status,
            error_code=locked.last_error_code,
            attempt=locked.attempts,
            transport_code=(
                "ACKNOWLEDGED"
                if result.ok
                else result.category.upper()
                if result.category in TRANSPORT_CODES
                else "UNCLASSIFIED_REJECTION"
            ),
        )
        return locked.status == "DELIVERED"


def _operator_authority(actor: User, reference: str) -> None:
    actor = User.objects.get(pk=actor.pk)
    if (
        not actor.is_active
        or not actor.is_staff
        or not actor.has_perm("arena.control_economic_automation")
    ):
        raise PermissionDenied("Economic operator authority required")
    if not reference.strip() or len(reference) > 255:
        raise ValidationError("ECONOMIC_ALERT_REVIEW_REFERENCE_REQUIRED")


@transaction.atomic
def review_delivery(
    delivery_id: int,
    *,
    actor: User,
    reference: str,
    action: str,
    dry_run: bool = True,
    message_id: int | None = None,
) -> dict[str, Any]:
    _operator_authority(actor, reference)
    row = (
        EconomicAlertDelivery.objects.select_for_update()
        .select_related("event")
        .get(pk=delivery_id)
    )
    if row.channel != CHANNEL:
        raise ValidationError("LEGACY_TRANSPORT_CANNOT_BE_REACTIVATED")
    if action == "RETRY_DEAD" and row.status == "DEAD":
        status = "PENDING"
    elif row.status == "UNCERTAIN" and action in {
        "CONFIRM_DELIVERED",
        "CONFIRM_NOT_SENT_AND_RETRY",
        "ABANDON",
    }:
        if action == "CONFIRM_DELIVERED" and (type(message_id) is not int or message_id <= 0):
            raise ValidationError("CONFIRMED_TELEGRAM_MESSAGE_ID_REQUIRED")
        status = {
            "CONFIRM_DELIVERED": "DELIVERED",
            "CONFIRM_NOT_SENT_AND_RETRY": "PENDING",
            "ABANDON": "DEAD",
        }[action]
    else:
        raise ValidationError("ECONOMIC_ALERT_REVIEW_STATE_OR_ACTION_INVALID")
    if status == "PENDING" and (row.telegram_user_id is None or row.telegram_user_id <= 0):
        raise ValidationError("PRIVATE_ALERT_TARGET_MUST_BE_POSITIVE")
    if row.event.outcome == "TEST_REQUESTED" and status == "PENDING":
        status = "HELD"  # A reviewed test still requires a separate explicit send authorization.
    if not dry_run:
        previous_attempts = row.attempts
        if status == "PENDING" or status == "HELD":
            row.retry_cycle_start = row.attempts
        row.status, row.next_attempt_at = status, timezone.now()
        row.lease_token = row.lease_expires_at = None
        if action == "CONFIRM_DELIVERED":
            row.telegram_message_id, row.delivered_at = message_id, timezone.now()
        row.save()
        _audit(
            row,
            "RETRY_REQUESTED" if action == "RETRY_DEAD" else "UNCERTAIN_REVIEW",
            actor_id=actor.pk,
            reference_digest=digest(reference),
            action=action,
            previous_attempts=previous_attempts,
            resulting_status=status,
        )
    return {
        "dry_run": dry_run,
        "delivery_id": row.pk,
        "action": action,
        "resulting_status": status,
        "attempts": row.attempts,
    }


def retry_dead_delivery(
    delivery_id: int, *, actor: User, reference: str, dry_run: bool = True
) -> dict[str, Any]:
    return review_delivery(
        delivery_id, actor=actor, reference=reference, action="RETRY_DEAD", dry_run=dry_run
    )


@transaction.atomic
def prepare_test_delivery(
    event_id: int, *, actor: User, reference: str, dry_run: bool = True
) -> dict[str, Any]:
    _operator_authority(actor, reference)
    operators = [op for op in authorized_operators() if op.user_id == actor.pk]
    if not operators:
        raise ValidationError("ACTIVE_REGISTERED_ADMIN_REQUIRED")
    event = EconomicAlertEvent.objects.select_related("alert__epoch").get(pk=event_id)
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "recipients": recipient_preview(),
        "requesting_operator_id": operators[0].pk,
        "message": format_message(event, preview_test=True),
    }
    if not dry_run:
        # Serialize repeated requests and preserve idempotent test history by actor/reference.
        EconomicAlertEvent.objects.select_for_update().get(pk=event.pk)
        evidence = {
            "source_event_id": event.pk,
            "actor_id": actor.pk,
            "reference_digest": digest(reference),
        }
        test, _ = EconomicAlertEvent.objects.get_or_create(
            alert_id=event.alert_id,
            outcome="TEST_REQUESTED",
            severity=event.severity,
            evidence=evidence,
        )
        report["delivery_ids"] = materialize_event(test)
        EconomicAlertDelivery.objects.filter(
            pk__in=report["delivery_ids"], status="PENDING"
        ).update(status="HELD")
        report["message"] = format_message(test)
    return report


def send_test_delivery(delivery_id: int, *, actor: User, reference: str) -> bool:
    """Release one held all-admin test recipient after explicit test authorization."""
    _operator_authority(actor, reference)
    if not settings.ECONOMIC_ALERT_DELIVERY_ENABLED:
        raise ValidationError("ECONOMIC_ALERT_DELIVERY_DISABLED")
    with transaction.atomic():
        row = (
            EconomicAlertDelivery.objects.select_for_update()
            .select_related("event")
            .get(pk=delivery_id)
        )
        if row.event.outcome != "TEST_REQUESTED" or row.event.evidence.get("actor_id") != actor.pk:
            raise PermissionDenied("Only the original requester may release a test")
        if not _authorized(row):
            if row.status == "HELD":
                row.status, row.last_error_code = "DEAD", "RECIPIENT_AUTHORITY_REVOKED"
                row.save(update_fields=("status", "last_error_code"))
                _audit(row, "DELIVERY_RESULT", status=row.status, error_code=row.last_error_code)
            return False
        if row.status == "HELD":
            row.status = "PENDING"
            row.save(update_fields=("status",))
            _audit(row, "TEST_AUTHORIZED", actor_id=actor.pk, reference_digest=digest(reference))
    return deliver_alert(row.pk)
