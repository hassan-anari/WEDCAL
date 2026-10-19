"""Private, database-authorized economic Telegram outbox. Never changes economics."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.arena.models import EconomicAlertDelivery, EconomicAlertEvent
from apps.arena.policy import digest
from apps.notifications.models import BotAdminOperator
from apps.players.models import Player
from apps.telegram import bot_api

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
    """Broadcast allowlists/permissions confer no economic alert authority."""
    contact = Player.objects.filter(
        telegram_user_id=OuterRef("telegram_user_id"), first_private_contact_at__isnull=False
    )
    permission = Permission.objects.filter(
        content_type__app_label="arena",
        content_type__model="economicalert",
        codename="receive_economic_alert",
    )
    candidates = (
        BotAdminOperator.objects.filter(
            enabled=True, telegram_user_id__gt=0, user__is_active=True, user__is_staff=True
        )
        .select_related("user")
        .filter(
            Q(user__is_superuser=True)
            | Q(user__user_permissions__in=permission)
            | Q(user__groups__permissions__in=permission)
        )
        .distinct()
        .annotate(private_contact=Exists(contact))
        .order_by("pk")
    )
    return list(candidates)


def recipient_preview() -> list[dict[str, Any]]:
    return [
        {"operator_id": op.pk, "reachable": bool(op.private_contact)}  # type: ignore[attr-defined]
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
    only_operator: BotAdminOperator | None = None,
) -> list[int]:
    event = EconomicAlertEvent.objects.select_for_update().get(pk=event.pk)
    if event.outcome not in TRANSITIONS:
        raise ValidationError("ECONOMIC_ALERT_EVENT_NOT_DELIVERABLE")
    if event.outcome == "TEST_REQUESTED" and (
        only_operator is None or event.evidence.get("actor_id") != only_operator.user_id
    ):
        raise PermissionDenied("Test events require their original private operator")
    now = now or timezone.now()
    operators = authorized_operators()
    if only_operator is not None:
        operators = [op for op in operators if op.pk == only_operator.pk]
    if not operators:
        EconomicAlertEvent.objects.create(
            alert_id=event.alert_id,
            outcome="ROUTING_FAILED",
            severity=event.severity,
            evidence={
                "source_event_id": event.pk,
                "reason": "NO_AUTHORIZED_OPERATOR",
                "error_code": "NO_AUTHORIZED_OPERATOR",
            },
        )
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
        reachable = bool(op.private_contact)  # type: ignore[attr-defined]
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
                "status": "PENDING" if reachable else "DEAD",
                "last_error_code": "" if reachable else "PRIVATE_CONTACT_REQUIRED",
            },
        )
        if created and not reachable:
            _audit(row, "DELIVERY_RESULT", status="DEAD", error_code="PRIVATE_CONTACT_REQUIRED")
        ids.append(row.pk)
    return ids


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
    # Reload rather than trusting a cached User permission result or queued identity snapshot.
    return any(
        op.pk == row.operator_id
        and op.telegram_user_id == row.telegram_user_id
        and bool(op.private_contact)  # type: ignore[attr-defined]
        for op in authorized_operators()
    )


def recover_expired_deliveries(*, now: datetime | None = None) -> int:
    """Expired claims may have sent. Never put them back in the automatic send queue."""
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
            row.status, row.last_error_code = "UNCERTAIN", "SEND_LEASE_EXPIRED"
            row.save(update_fields=("status", "last_error_code"))
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
            .filter(pk=delivery_id, channel=CHANNEL, status="PENDING", next_attempt_at__lte=now)
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
            locked.status, locked.last_error_code = "UNCERTAIN", "AMBIGUOUS_SEND_OUTCOME"
        elif result.category in {
            "forbidden",
            "chat_not_found",
            "recipient_authority_revoked",
            "configuration_error",
        }:
            locked.status, locked.last_error_code = "DEAD", str(result.category).upper()
        else:
            # Only explicit non-ambiguous rejections are retried. Unknown failures fail closed.
            safe_retry = result.category in {
                "rate_limited",
                "local_rate_limited",
                "budget_unavailable",
            }
            locked.status = (
                "PENDING"
                if safe_retry and locked.attempts < settings.ECONOMIC_ALERT_MAX_ATTEMPTS
                else "DEAD"
            )
            locked.last_error_code = (
                str(result.category).upper() if safe_retry else "TRANSPORT_REJECTED"
            )
            backoff = min(
                settings.ECONOMIC_ALERT_RETRY_MAX_SECONDS,
                settings.ECONOMIC_ALERT_RETRY_BASE_SECONDS
                * 2
                ** min(locked.attempts - 1, settings.ECONOMIC_ALERT_RETRY_MAX_SECONDS.bit_length()),
            )
            # Telegram retry_after is a minimum; never cap it below the server's instruction.
            locked.next_attempt_at = timezone.now() + timedelta(
                seconds=max(result.retry_after or 0, backoff)
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
    if row.event.outcome == "TEST_REQUESTED" and status == "PENDING":
        status = "HELD"  # A reviewed test still requires a separate explicit send authorization.
    if not dry_run:
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
    fresh_actor = User.objects.get(pk=actor.pk)
    if not fresh_actor.has_perm("arena.test_economic_alert"):
        raise PermissionDenied("Economic alert test permission required")
    operators = [op for op in authorized_operators() if op.user_id == actor.pk]
    if not operators or not operators[0].private_contact:  # type: ignore[attr-defined]
        raise ValidationError("AUTHORIZED_OPERATOR_PRIVATE_CONTACT_REQUIRED")
    event = EconomicAlertEvent.objects.select_related("alert__epoch").get(pk=event_id)
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "recipients": recipient_preview(),
        "test_operator_id": operators[0].pk,
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
        report["delivery_ids"] = materialize_event(test, only_operator=operators[0])
        EconomicAlertDelivery.objects.filter(
            pk__in=report["delivery_ids"], status="PENDING"
        ).update(status="HELD")
        report["message"] = format_message(test)
    return report


def send_test_delivery(delivery_id: int, *, actor: User, reference: str) -> bool:
    """Explicit release for one previously held test; never called by scheduled workers."""
    _operator_authority(actor, reference)
    fresh = User.objects.get(pk=actor.pk)
    if not fresh.has_perm("arena.test_economic_alert"):
        raise PermissionDenied("Economic alert test permission required")
    if not settings.ECONOMIC_ALERT_DELIVERY_ENABLED:
        raise ValidationError("ECONOMIC_ALERT_DELIVERY_DISABLED")
    with transaction.atomic():
        row = (
            EconomicAlertDelivery.objects.select_for_update()
            .select_related("event")
            .get(pk=delivery_id)
        )
        if row.event.outcome != "TEST_REQUESTED" or row.event.evidence.get("actor_id") != actor.pk:
            raise PermissionDenied("Only the original operator may release a private test")
        if not _authorized(row):
            raise PermissionDenied("Private operator authority required")
        if row.status == "HELD":
            row.status = "PENDING"
            row.save(update_fields=("status",))
            _audit(row, "TEST_AUTHORIZED", actor_id=actor.pk, reference_digest=digest(reference))
    return deliver_alert(row.pk)
