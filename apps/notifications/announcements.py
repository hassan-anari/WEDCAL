"""Durable In-App publishing; a batch commits notifications and its cursor together."""

import hashlib
import json
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.notifications.models import InAppAnnouncement, OperatorAuditEvent
from apps.notifications.services import emit_notification as emit_notification
from apps.players.models import Player


def fingerprint(row: InAppAnnouncement) -> str:
    return hashlib.sha256(
        json.dumps(
            [row.template_id, row.audience, row.player_id, row.template.title, row.template.body],
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


def preview(row: InAppAnnouncement) -> dict[str, Any]:
    row.full_clean()
    if not row.template.enabled:
        raise ValidationError("Select an enabled In-App template")
    upper = Player.objects.aggregate(v=Max("pk"))["v"] or 0
    query = Player.objects.filter(pk__lte=upper)
    if row.audience == "ONE":
        assert row.player_id is not None
        query = query.filter(pk=row.player_id)
    return {"fingerprint": fingerprint(row), "upper": upper, "count": query.count()}


def _permission(actor: Any, permission: str) -> None:
    if not actor.is_active or not actor.has_perm(f"notifications.{permission}_inappannouncement"):
        raise PermissionDenied


def _audit(row: InAppAnnouncement, actor: Any, action: str, reason: str) -> None:
    OperatorAuditEvent.objects.create(
        actor=actor,
        action=f"ANNOUNCEMENT_{action}",
        target_type="InAppAnnouncement",
        target_id=str(row.public_id),
        reason=reason,
        after_state={
            "state": row.state,
            "processed_count": row.processed_count,
            "estimated_recipients": row.estimated_recipients,
        },
    )


@transaction.atomic
def confirm(row_id: int, actor: Any, *, expected: dict[str, Any], reason: str) -> InAppAnnouncement:
    _permission(actor, "publish")
    row = InAppAnnouncement.objects.select_for_update().select_related("template").get(pk=row_id)
    if row.state != "DRAFT":
        return row  # A repeated signed confirmation never starts a second job.
    row.full_clean()
    if (
        not reason.strip()
        or fingerprint(row) != expected["fingerprint"]
        or not row.template.enabled
    ):
        raise ValidationError("Draft changed; preview again before confirming")
    row.title_snapshot, row.body_snapshot = row.template.title, row.template.body
    row.recipient_upper_bound, row.estimated_recipients = expected["upper"], expected["count"]
    row.confirmed_by, row.confirmed_at, row.state = actor, timezone.now(), "PENDING"
    row.save()
    _audit(row, actor, "CONFIRM", reason)
    return row


@transaction.atomic
def cancel(row_id: int, actor: Any, *, reason: str) -> InAppAnnouncement:
    _permission(actor, "cancel")
    row = InAppAnnouncement.objects.select_for_update().get(pk=row_id)
    if not reason.strip():
        raise ValidationError("A cancellation reason is required")
    if row.state not in {"CANCELLED", "COMPLETED"}:
        row.state, row.cancelled_at = "CANCELLED", timezone.now()
        row.save(update_fields=("state", "cancelled_at"))
        _audit(row, actor, "CANCEL", reason)
    return row


def process_batch(row_id: int, *, limit: int | None = None) -> int:
    size = min(max(limit or settings.ANNOUNCEMENT_BATCH_SIZE, 1), 500)
    try:
        with transaction.atomic():
            row = (
                InAppAnnouncement.objects.select_for_update(skip_locked=True)
                .filter(pk=row_id, state__in=["PENDING", "PROCESSING"])
                .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now()))
                .first()
            )
            if row is None:
                return 0
            recipients = Player.objects.filter(
                pk__gt=row.cursor, pk__lte=row.recipient_upper_bound
            ).order_by("pk")
            if row.audience == "ONE":
                assert row.player_id is not None
                recipients = recipients.filter(pk=row.player_id)
            batch = list(recipients[:size])
            for recipient in batch:
                emit_notification(
                    recipient=recipient,
                    event_key="IN_APP_ANNOUNCEMENT",
                    source_domain="notifications",
                    source_type="IN_APP_ANNOUNCEMENT",
                    source_id=str(row.public_id),
                    idempotency_key=f"announcement:{row.public_id}:{recipient.pk}",
                    title=row.title_snapshot,
                    body=row.body_snapshot,
                    template_snapshot=row.template,
                    telegram=False,
                )
            if batch:
                row.cursor = batch[-1].pk
            row.processed_count += len(batch)
            row.attempt_count, row.next_attempt_at, row.error_category = 0, None, ""
            row.state = "PROCESSING" if len(batch) == size else "COMPLETED"
            if row.state == "COMPLETED":
                row.completed_at = timezone.now()
            row.save()
            return len(batch)
    except Exception:
        # Rolled-back batch leaves both cursor and recipient notifications untouched.
        # No exception text/content is retained. Beat recovers worker/broker loss.
        with transaction.atomic():
            row = InAppAnnouncement.objects.select_for_update().get(pk=row_id)
            if row.state not in {"PENDING", "PROCESSING"}:
                return 0
            row.attempt_count += 1
            row.error_category = "batch_transaction_failed"
            row.next_attempt_at = timezone.now() + timedelta(seconds=min(300, 2**row.attempt_count))
            if row.attempt_count >= settings.ANNOUNCEMENT_MAX_ATTEMPTS:
                row.state = "FAILED"
            row.save(update_fields=("attempt_count", "error_category", "next_attempt_at", "state"))
        return 0


@transaction.atomic
def retry(row_id: int, actor: Any, *, reason: str) -> InAppAnnouncement:
    _permission(actor, "publish")
    row = InAppAnnouncement.objects.select_for_update().get(pk=row_id)
    if row.state != "FAILED" or not reason.strip():
        raise ValidationError("Only a failed announcement can be retried with a reason")
    row.state, row.attempt_count, row.next_attempt_at, row.error_category = "PENDING", 0, None, ""
    row.save(update_fields=("state", "attempt_count", "next_attempt_at", "error_category"))
    _audit(row, actor, "RETRY", reason)
    return row
