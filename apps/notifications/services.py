# mypy: disable-error-code="return-value,union-attr,operator"
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
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


def _signature(notification: Notification) -> tuple[object, ...]:
    return (
        notification.recipient_id,
        notification.event_key,
        notification.source_domain,
        notification.source_type,
        notification.source_id,
        notification.title,
        notification.body,
        notification.payload,
    )


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
    template = (
        NotificationTemplate.objects.filter(
            event_key=event_key, channel=NotificationTemplate.Channel.IN_APP, enabled=True
        )
        .order_by("-version")
        .first()
    )
    if template:
        title, body = template.title, template.body
    expected = (
        recipient.pk,
        event_key,
        source_domain,
        source_type,
        source_id,
        title,
        body,
        safe_payload,
    )
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
                telegram_template = (
                    NotificationTemplate.objects.filter(
                        event_key=event_key,
                        channel=NotificationTemplate.Channel.TELEGRAM,
                        enabled=True,
                    )
                    .order_by("-version")
                    .first()
                )
                text = telegram_template.body if telegram_template else ""
                TelegramDelivery.objects.create(
                    notification=notification,
                    telegram_user_id=recipient.telegram_user_id,
                    text_snapshot=text,
                    state=DeliveryState.PENDING
                    if recipient.first_private_contact_at and text
                    else DeliveryState.SKIPPED,
                    error_category=""
                    if recipient.first_private_contact_at and text
                    else "private_contact_or_template_unavailable",
                    completed_at=None
                    if recipient.first_private_contact_at and text
                    else timezone.now(),
                )
            return notification, True
    except IntegrityError:
        existing = Notification.objects.get(idempotency_key=idempotency_key)
        if _signature(existing) != expected:
            raise NotificationIdempotencyConflict("Notification idempotency key conflict") from None
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
        return len(batch)


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


def deliver_broadcasts(limit: int | None = None) -> int:
    rows = _claim(BroadcastRecipient, limit=limit or settings.NOTIFICATION_DELIVERY_BATCH_SIZE)
    rows = [
        row
        for row in rows
        if row.campaign.state in {BroadcastCampaign.State.QUEUED, BroadcastCampaign.State.SENDING}
    ]
    for row in rows:
        _deliver(row, row.campaign.body_snapshot)
        BroadcastCampaign.objects.filter(
            pk=row.campaign_id, state=BroadcastCampaign.State.QUEUED
        ).update(state=BroadcastCampaign.State.SENDING)
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
