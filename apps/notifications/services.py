# mypy: disable-error-code="return-value,union-attr,operator"
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from apps.notifications.models import (
    BroadcastCampaign,
    BroadcastRecipient,
    DeliveryState,
    Notification,
    NotificationTemplate,
    OperatorAuditEvent,
    TelegramDelivery,
)
from apps.players.models import Player
from apps.telegram import bot_api


class NotificationIdempotencyConflict(ValidationError):
    pass


def emit_notification_safely(**kwargs: Any) -> None:
    """Best-effort seam used after a domain transaction has durably committed."""
    try:
        emit_notification(**kwargs)
    except Exception:
        import structlog

        structlog.get_logger(__name__).exception(
            "notification_emit_failed",
            event_key=kwargs.get("event_key"),
            source_id=kwargs.get("source_id"),
        )


def _validate_existing_event(
    notification: Notification,
    *,
    recipient: Player,
    event_key: str,
    source_domain: str,
    source_type: str,
    source_id: str,
    payload: dict[str, Any],
    explicit_title: str,
    explicit_body: str,
) -> None:
    semantics = (
        notification.recipient_id,
        notification.event_key,
        notification.source_domain,
        notification.source_type,
        notification.source_id,
        notification.payload,
    )
    expected = (recipient.pk, event_key, source_domain, source_type, source_id, payload)
    if (
        semantics != expected
        or (explicit_title and notification.title != explicit_title)
        or (explicit_body and notification.body != explicit_body)
    ):
        raise NotificationIdempotencyConflict("Notification idempotency key conflict")


def _ensure_telegram_delivery(notification: Notification, recipient: Player) -> TelegramDelivery:
    telegram_template = (
        NotificationTemplate.objects.filter(
            event_key=notification.event_key,
            channel=NotificationTemplate.Channel.TELEGRAM,
            enabled=True,
        )
        .order_by("-version")
        .first()
    )
    text = telegram_template.body if telegram_template else ""
    available = bool(recipient.first_private_contact_at and text)
    delivery, _ = TelegramDelivery.objects.get_or_create(
        notification=notification,
        defaults={
            "telegram_user_id": recipient.telegram_user_id,
            "text_snapshot": text,
            "state": DeliveryState.PENDING if available else DeliveryState.SKIPPED,
            "error_category": "" if available else "private_contact_or_template_unavailable",
            "completed_at": None if available else timezone.now(),
        },
    )
    return delivery


def emit_notification(
    *,
    recipient: Player,
    event_key: str,
    source_domain: str,
    source_type: str,
    source_id: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    title: str = "",
    body: str = "",
    telegram: bool = False,
) -> tuple[Notification, bool]:
    safe_payload = payload or {}
    existing = Notification.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        _validate_existing_event(
            existing,
            recipient=recipient,
            event_key=event_key,
            source_domain=source_domain,
            source_type=source_type,
            source_id=source_id,
            payload=safe_payload,
            explicit_title=title,
            explicit_body=body,
        )
        if telegram:
            with transaction.atomic():
                _ensure_telegram_delivery(existing, recipient)
        return existing, False
    template = (
        NotificationTemplate.objects.filter(
            event_key=event_key, channel=NotificationTemplate.Channel.IN_APP, enabled=True
        )
        .order_by("-version")
        .first()
    )
    if template:
        title, body = template.title, template.body
    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                recipient=recipient,
                event_key=event_key,
                source_domain=source_domain,
                source_type=source_type,
                source_id=source_id,
                idempotency_key=idempotency_key,
                title=title,
                body=body,
                payload=safe_payload,
                template=template,
            )
            if telegram:
                _ensure_telegram_delivery(notification, recipient)
            return notification, True
    except IntegrityError:
        existing = Notification.objects.get(idempotency_key=idempotency_key)
        _validate_existing_event(
            existing,
            recipient=recipient,
            event_key=event_key,
            source_domain=source_domain,
            source_type=source_type,
            source_id=source_id,
            payload=safe_payload,
            explicit_title=title,
            explicit_body=body,
        )
        if telegram:
            with transaction.atomic():
                _ensure_telegram_delivery(existing, recipient)
        return existing, False


@transaction.atomic
def mark_read(*, recipient: Player, public_id: uuid.UUID) -> Notification:
    notification = Notification.objects.select_for_update().get(
        public_id=public_id, recipient=recipient
    )
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=("read_at",))
    return notification


Delivery = TelegramDelivery | BroadcastRecipient


def _claim(
    model: type[TelegramDelivery] | type[BroadcastRecipient], *, limit: int
) -> list[Delivery]:
    now = timezone.now()
    with transaction.atomic():
        rows = list(
            model.objects.select_for_update(skip_locked=True)
            .filter(
                Q(state=DeliveryState.PENDING)
                | Q(state=DeliveryState.RETRY_WAIT, next_attempt_at__lte=now)
                | Q(state=DeliveryState.PROCESSING, lease_expires_at__lte=now)
            )
            .order_by("id")[:limit]
        )
        for row in rows:
            row.state = DeliveryState.PROCESSING
            row.lease_token = uuid.uuid4()
            row.lease_expires_at = now + timedelta(
                seconds=settings.NOTIFICATION_DELIVERY_LEASE_SECONDS
            )
            row.attempt_count += 1
            row.save(update_fields=("state", "lease_token", "lease_expires_at", "attempt_count"))
    return rows


def _deliver(row: Delivery, text: str) -> None:
    token = row.lease_token
    result = bot_api.send_message(row.telegram_user_id, text)
    now = timezone.now()
    with transaction.atomic():
        locked = type(row).objects.select_for_update().get(pk=row.pk)
        if locked.state != DeliveryState.PROCESSING or locked.lease_token != token:
            return
        locked.lease_token = locked.lease_expires_at = None
        if result.ok:
            locked.state, locked.sent_at, locked.completed_at = DeliveryState.SENT, now, now
            message_id = (result.result or {}).get("message_id")
            locked.telegram_message_id = message_id if type(message_id) is int else None
        elif (
            result.category == "rate_limited"
            and result.retry_after is not None
            and locked.attempt_count < settings.NOTIFICATION_MAX_ATTEMPTS
        ):
            locked.state = DeliveryState.RETRY_WAIT
            locked.next_attempt_at = now + timedelta(seconds=result.retry_after)
            locked.error_category = "rate_limited"
        elif result.ambiguous:
            locked.state, locked.completed_at = DeliveryState.UNCERTAIN, now
            locked.error_category = result.category or "ambiguous"
        else:
            locked.state, locked.completed_at = DeliveryState.FAILED, now
            locked.error_category = result.category or "terminal"
        locked.save()


def deliver_notifications(limit: int | None = None) -> int:
    rows = _claim(TelegramDelivery, limit=limit or settings.NOTIFICATION_DELIVERY_BATCH_SIZE)
    for row in rows:
        _deliver(row, row.text_snapshot)
    return len(rows)


@transaction.atomic
def queue_campaign(*, campaign: BroadcastCampaign, actor: Any, reason: str) -> BroadcastCampaign:
    campaign = BroadcastCampaign.objects.select_for_update().get(pk=campaign.pk)
    if campaign.state == BroadcastCampaign.State.MATERIALIZING:
        return campaign
    if campaign.state != BroadcastCampaign.State.DRAFT or not reason.strip():
        raise ValidationError("Only a draft campaign with a reason can be queued")
    campaign.state = BroadcastCampaign.State.MATERIALIZING
    campaign.queued_at = timezone.now()
    campaign.save(update_fields=("state", "queued_at"))
    OperatorAuditEvent.objects.create(
        actor=actor,
        action="BROADCAST_QUEUE",
        target_type="BroadcastCampaign",
        target_id=str(campaign.public_id),
        reason=reason,
        before_state={"state": "DRAFT"},
        after_state={"state": campaign.state},
    )
    return campaign


def materialize_campaign(campaign_id: int, batch_size: int = 1000) -> int:
    with transaction.atomic():
        campaign = BroadcastCampaign.objects.select_for_update().get(pk=campaign_id)
        if campaign.state != BroadcastCampaign.State.MATERIALIZING:
            return 0
        players = Player.objects.filter(pk__gt=campaign.materialization_cursor).order_by("pk")
        if campaign.audience_type == BroadcastCampaign.Audience.TELEGRAM_REACHABLE:
            players = players.filter(first_private_contact_at__isnull=False)
        batch = list(players[:batch_size])
        BroadcastRecipient.objects.bulk_create(
            [
                BroadcastRecipient(
                    campaign=campaign,
                    player=p,
                    telegram_user_id=p.telegram_user_id,
                    state=DeliveryState.PENDING
                    if p.first_private_contact_at
                    else DeliveryState.SKIPPED,
                    completed_at=None if p.first_private_contact_at else timezone.now(),
                    error_category=""
                    if p.first_private_contact_at
                    else "private_contact_unavailable",
                )
                for p in batch
            ],
            ignore_conflicts=True,
        )
        if batch:
            campaign.materialization_cursor = batch[-1].pk
        if len(batch) < batch_size:
            campaign.state = BroadcastCampaign.State.QUEUED
        campaign.save(update_fields=("materialization_cursor", "state"))
        count = len(batch)
    reconcile_campaign(campaign_id)
    return count


@transaction.atomic
def cancel_campaign(*, campaign: BroadcastCampaign, actor: Any, reason: str) -> BroadcastCampaign:
    campaign = BroadcastCampaign.objects.select_for_update().get(pk=campaign.pk)
    if campaign.state in {BroadcastCampaign.State.COMPLETED, BroadcastCampaign.State.CANCELLED}:
        return campaign
    if not reason.strip():
        raise ValidationError("Cancellation reason is required")
    previous = campaign.state
    campaign.state, campaign.cancelled_at = BroadcastCampaign.State.CANCELLED, timezone.now()
    campaign.save(update_fields=("state", "cancelled_at"))
    campaign.recipients.filter(state__in=(DeliveryState.PENDING, DeliveryState.RETRY_WAIT)).update(
        state=DeliveryState.SKIPPED,
        completed_at=timezone.now(),
        error_category="campaign_cancelled",
    )
    OperatorAuditEvent.objects.create(
        actor=actor,
        action="BROADCAST_CANCEL",
        target_type="BroadcastCampaign",
        target_id=str(campaign.public_id),
        reason=reason,
        before_state={"state": previous},
        after_state={"state": campaign.state},
    )
    return campaign


TERMINAL_DELIVERY_STATES = {
    DeliveryState.SENT,
    DeliveryState.FAILED,
    DeliveryState.UNCERTAIN,
    DeliveryState.SKIPPED,
}
SENDABLE_CAMPAIGN_STATES = {
    BroadcastCampaign.State.QUEUED,
    BroadcastCampaign.State.SENDING,
}


@transaction.atomic
def claim_broadcast_recipients(limit: int) -> list[BroadcastRecipient]:
    """Campaign lock wins before recipient locks; committed PROCESSING means in-flight."""
    now = timezone.now()
    claimable = BroadcastRecipient.objects.filter(campaign_id=OuterRef("pk")).filter(
        Q(state=DeliveryState.PENDING)
        | Q(state=DeliveryState.RETRY_WAIT, next_attempt_at__lte=now)
        | Q(state=DeliveryState.PROCESSING, lease_expires_at__lte=now)
    )
    campaign = (
        BroadcastCampaign.objects.select_for_update(skip_locked=True)
        .filter(state__in=SENDABLE_CAMPAIGN_STATES)
        .annotate(has_claimable=Exists(claimable))
        .filter(has_claimable=True)
        .order_by("id")
        .first()
    )
    if campaign is None:
        return []
    rows = list(
        BroadcastRecipient.objects.select_for_update(skip_locked=True)
        .filter(campaign=campaign)
        .filter(
            Q(state=DeliveryState.PENDING)
            | Q(state=DeliveryState.RETRY_WAIT, next_attempt_at__lte=now)
            | Q(state=DeliveryState.PROCESSING, lease_expires_at__lte=now)
        )
        .order_by("id")[:limit]
    )
    for row in rows:
        row.state = DeliveryState.PROCESSING
        row.lease_token = uuid.uuid4()
        row.lease_expires_at = now + timedelta(seconds=settings.NOTIFICATION_DELIVERY_LEASE_SECONDS)
        row.attempt_count += 1
        row.save(update_fields=("state", "lease_token", "lease_expires_at", "attempt_count"))
    return rows


@transaction.atomic
def reconcile_campaign(campaign_id: int) -> BroadcastCampaign:
    campaign = BroadcastCampaign.objects.select_for_update().get(pk=campaign_id)
    if campaign.state == BroadcastCampaign.State.CANCELLED:
        campaign.recipients.filter(
            state=DeliveryState.PROCESSING, lease_expires_at__lte=timezone.now()
        ).update(
            state=DeliveryState.UNCERTAIN,
            completed_at=timezone.now(),
            error_category="cancelled_inflight_lease_expired",
            lease_token=None,
            lease_expires_at=None,
        )
        return campaign
    if campaign.state not in SENDABLE_CAMPAIGN_STATES:
        return campaign
    if not campaign.recipients.exclude(state__in=TERMINAL_DELIVERY_STATES).exists():
        campaign.state = BroadcastCampaign.State.COMPLETED
        campaign.completed_at = timezone.now()
        campaign.save(update_fields=("state", "completed_at"))
    return campaign


def deliver_broadcasts(limit: int | None = None) -> int:
    rows = claim_broadcast_recipients(limit or settings.NOTIFICATION_DELIVERY_BATCH_SIZE)
    campaign_ids = {row.campaign_id for row in rows}
    for row in rows:
        _deliver(row, row.campaign.body_snapshot)
        BroadcastCampaign.objects.filter(
            pk=row.campaign_id, state=BroadcastCampaign.State.QUEUED
        ).update(state=BroadcastCampaign.State.SENDING)
    for campaign_id in campaign_ids:
        reconcile_campaign(campaign_id)
    return len(rows)


def campaign_metrics(campaign: BroadcastCampaign) -> dict[str, int]:
    counts = {
        item["state"]: item["count"]
        for item in campaign.recipients.values("state").annotate(count=Count("id"))
    }
    return {
        "total": sum(counts.values()),
        **{state.lower(): counts.get(state, 0) for state in DeliveryState.values},
    }
