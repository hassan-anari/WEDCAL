# mypy: disable-error-code="return-value,union-attr,operator"
import random
import uuid
from datetime import timedelta
from typing import Any, cast

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

from apps.notifications.audiences import (
    eligible_groups,
    eligible_users,
    estimated_targets,
    verify_broadcast_group,
)
from apps.notifications.clan_copy import default_clan_copy
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
from apps.telegram.models import TelegramGroup


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
    template_snapshot: NotificationTemplate | None = None,
) -> tuple[Notification, bool]:
    if template_snapshot is not None and (telegram or template_snapshot.channel != "IN_APP"):
        raise ValidationError("Explicit announcement snapshots are In-App only")
    safe_payload = payload or {}
    # Presentation defaults/templates must never become caller-supplied identity.
    explicit_title, explicit_body = title, body
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
    template = template_snapshot or (
        NotificationTemplate.objects.filter(
            event_key=event_key, channel=NotificationTemplate.Channel.IN_APP, enabled=True
        )
        .order_by("-version")
        .first()
    )
    if template and template_snapshot is None:
        title, body = template.title, template.body
    elif template is None:
        default_title, default_body = default_clan_copy(event_key, source_domain)
        title, body = title or default_title, body or default_body
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
            explicit_title=explicit_title,
            explicit_body=explicit_body,
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
    model: type[TelegramDelivery] | type[BroadcastRecipient],
    *,
    limit: int,
    delivery_id: int | None = None,
) -> list[Delivery]:
    now = timezone.now()
    with transaction.atomic():
        query = model.objects.all()
        if delivery_id is not None:
            query = query.filter(pk=delivery_id)
        rows = list(
            query.select_for_update(skip_locked=True)
            .filter(
                Q(state=DeliveryState.PENDING)
                | Q(state=DeliveryState.RETRY_WAIT, next_attempt_at__lte=now)
                | Q(state=DeliveryState.PROCESSING, lease_expires_at__lte=now)
            )
            .order_by("id")[:limit]
        )
        for row in rows:
            if row.state == DeliveryState.PROCESSING:
                row.state = DeliveryState.UNCERTAIN
                row.completed_at = now
                row.error_category = "dispatch_lease_expired"
                row.lease_token = row.lease_expires_at = None
                row.save()
                continue
            row.state = DeliveryState.PROCESSING
            row.lease_token = uuid.uuid4()
            row.lease_expires_at = now + timedelta(
                seconds=settings.NOTIFICATION_DELIVERY_LEASE_SECONDS
            )
            row.attempt_count += 1
            row.save(update_fields=("state", "lease_token", "lease_expires_at", "attempt_count"))
    return [cast(Delivery, row) for row in rows if row.state == DeliveryState.PROCESSING]


def _private_control_permitted(row: TelegramDelivery) -> bool:
    from apps.notifications.models import BotAdminOperator

    notification = row.notification
    if notification.event_key != "BOT_PRIVATE":
        return True
    key = notification.idempotency_key
    if key.startswith("bot-welcome:"):
        return bool(settings.B3_BOT_WELCOME_ENABLED and settings.B4_PUBLIC_WELCOME_ENABLED)
    if key.startswith(("bot-panel:", "bot-ack:", "welcome-test:")):
        operator = (
            BotAdminOperator.objects.select_related("user")
            .filter(telegram_user_id=row.telegram_user_id, enabled=True)
            .first()
        )
        permission = "test_welcome" if key.startswith("welcome-test:") else "manage_broadcasts"
        return bool(
            operator
            and row.telegram_user_id in settings.TELEGRAM_ADMIN_IDS
            and operator.user.is_active
            and operator.user.has_perm(f"notifications.{permission}")
            and (permission == "test_welcome" or settings.B3_BROADCAST_AUDIENCES_ENABLED)
        )
    return True


def _deliver(row: Delivery, text: str) -> None:
    if transaction.get_connection().in_atomic_block:
        raise RuntimeError("Telegram delivery must run outside business transactions")
    started_at = timezone.now()
    token = row.lease_token
    # Fence legacy queued/retry credentials as well as new invite creation.
    if isinstance(row, TelegramDelivery) and row.notification.source_type == "CLAN_INVITE":
        TelegramDelivery.objects.filter(
            pk=row.pk, lease_token=token, state=DeliveryState.PROCESSING
        ).update(
            state=DeliveryState.SKIPPED,
            completed_at=timezone.now(),
            lease_token=None,
            lease_expires_at=None,
            error_category="invite_miniapp_only",
        )
        return
    if isinstance(row, TelegramDelivery) and not _private_control_permitted(row):
        TelegramDelivery.objects.filter(
            pk=row.pk, lease_token=token, state=DeliveryState.PROCESSING
        ).update(
            state=DeliveryState.SKIPPED,
            completed_at=timezone.now(),
            lease_token=None,
            lease_expires_at=None,
            error_category="operator_authorization_revoked",
        )
        return
    options = row.message_options if isinstance(row, TelegramDelivery) else {}

    def send_content() -> bot_api.TelegramResult:
        source = row.copy_source if isinstance(row, TelegramDelivery) else row.campaign.copy_source
        if source is not None:
            from apps.notifications.broadcast_content import copy

            return copy(
                source,
                row.telegram_user_id,
                preview=isinstance(row, TelegramDelivery),
                preview_delivery_id=row.pk if isinstance(row, TelegramDelivery) else None,
            )
        return bot_api.send_message(row.telegram_user_id, text)

    if isinstance(row, BroadcastRecipient) and row.target_kind == "GROUP":
        group_id = row.group_id
        if group_id is None and row.group_subscription_id:
            group_id = row.group_subscription.group_id
        assert group_id is not None  # Enforced by broadcast_exact_target for both layouts.
        try:
            group = TelegramGroup.objects.get(pk=group_id)
            permitted = group.telegram_chat_id == row.telegram_user_id and verify_broadcast_group(
                group
            )
        except ValidationError:
            result = bot_api.TelegramResult(False, category="local_rate_limited", retry_after=30)
        else:
            if not permitted:
                type(row).objects.filter(
                    pk=row.pk, lease_token=token, state=DeliveryState.PROCESSING
                ).update(
                    state=DeliveryState.SKIPPED,
                    error_category="group_permission_unavailable",
                    completed_at=timezone.now(),
                    lease_token=None,
                    lease_expires_at=None,
                )
                return
            result = send_content()
    elif (
        isinstance(row, BroadcastRecipient)
        and not eligible_users()
        .filter(pk=row.player_id, telegram_user_id=row.telegram_user_id)
        .exists()
    ):
        type(row).objects.filter(pk=row.pk, lease_token=token).update(
            state=DeliveryState.SKIPPED,
            completed_at=timezone.now(),
            lease_token=None,
            lease_expires_at=None,
            error_category="private_contact_unavailable",
        )
        return
    elif isinstance(row, TelegramDelivery) and row.copy_source_id:
        result = send_content()
    elif options.get("_method") == "answerCallbackQuery":
        result = bot_api.request(
            "answerCallbackQuery", {"callback_query_id": options["callback_query_id"]}
        )
    elif options:
        result = bot_api.request(
            "sendMessage", {"chat_id": row.telegram_user_id, "text": text, **options}
        )
    else:
        result = send_content()
    now = timezone.now()
    with transaction.atomic():
        locked = type(row).objects.select_for_update().get(pk=row.pk)
        if locked.state != DeliveryState.PROCESSING or locked.lease_token != token:
            return
        locked.lease_token = locked.lease_expires_at = None
        retry_deadline_passed = (
            now - locked.created_at
        ).total_seconds() > settings.NOTIFICATION_MAX_QUEUE_AGE_SECONDS
        locked.telegram_message_ids = (result.result or {}).get("message_ids", [])
        if result.ok:
            locked.state, locked.sent_at, locked.completed_at = DeliveryState.SENT, now, now
            message_id = (result.result or {}).get("message_id")
            locked.telegram_message_id = message_id if type(message_id) is int else None
        elif (
            result.category in {"local_rate_limited", "rate_limited", "budget_unavailable"}
            and retry_deadline_passed
        ):
            locked.state, locked.completed_at = DeliveryState.SKIPPED, now
            locked.error_category = "retry_deadline_exceeded"
        elif result.category in {"local_rate_limited", "rate_limited", "budget_unavailable"}:
            # Admission refusal never dispatched a message and is not a delivery failure.
            locked.state = DeliveryState.RETRY_WAIT
            delay = max(result.retry_after or 1, min(60, 2 ** min(locked.attempt_count, 6)))
            jitter = float(settings.TELEGRAM_API_RETRY_JITTER_SECONDS)
            if (
                isinstance(locked, TelegramDelivery)
                and locked.notification.event_key == "BOT_PRIVATE"
            ):
                delay = max(result.retry_after or 1, 1)
                jitter = settings.NOTIFICATION_INTERACTIVE_RETRY_JITTER_SECONDS
            locked.next_attempt_at = now + timedelta(seconds=delay + random.uniform(0, jitter))
            locked.error_category = result.category
            locked.attempt_count = max(0, locked.attempt_count - 1)
        elif result.category in {"forbidden", "chat_not_found"}:
            locked.state, locked.completed_at = DeliveryState.SKIPPED, now
            locked.error_category = result.category
        elif result.ambiguous:
            locked.state, locked.completed_at = DeliveryState.UNCERTAIN, now
            locked.error_category = result.category or "ambiguous"
        else:
            locked.state, locked.completed_at = DeliveryState.FAILED, now
            locked.error_category = result.category or "terminal"
        locked.save()
        if isinstance(locked, TelegramDelivery) and locked.copy_source_id and result.ok:
            from apps.notifications.models import BroadcastSource

            # CAS elects exactly one immutable preview even if previews complete
            # out of order. This happens in the same transaction as SENT.
            BroadcastSource.objects.filter(
                pk=locked.copy_source_id, frozen_preview__isnull=True
            ).update(frozen_preview=locked)
        if isinstance(locked, TelegramDelivery) and locked.state == DeliveryState.RETRY_WAIT:
            from apps.notifications.dispatch import dispatch_delivery

            dispatch_delivery(locked.pk, locked.next_attempt_at)
        if isinstance(locked, TelegramDelivery):
            import structlog

            origin = locked.source_received_at or locked.created_at
            structlog.get_logger(__name__).info(
                "telegram_interactive_delivery",
                delivery_id=locked.pk,
                method=locked.message_options.get("_method", "sendMessage"),
                outcome=locked.state,
                attempt=locked.attempt_count,
                queue_ms=round((started_at - locked.created_at).total_seconds() * 1000),
                delivery_ms=round((now - started_at).total_seconds() * 1000),
                end_to_end_ms=round((now - origin).total_seconds() * 1000),
            )


def deliver_one_notification(delivery_id: int) -> int:
    rows = _claim(TelegramDelivery, limit=1, delivery_id=delivery_id)
    for row in rows:
        _deliver(row, row.text_snapshot)
    return len(rows)


def deliver_notifications(limit: int | None = None) -> int:
    rows = _claim(TelegramDelivery, limit=limit or settings.NOTIFICATION_DELIVERY_BATCH_SIZE)
    for row in rows:
        _deliver(row, row.text_snapshot)
    return len(rows)


@transaction.atomic
def queue_campaign(*, campaign: BroadcastCampaign, actor: Any, reason: str) -> BroadcastCampaign:
    if not actor.is_active or not actor.has_perm("notifications.manage_broadcasts"):
        raise PermissionDenied("Broadcast operator authorization is required")
    campaign = BroadcastCampaign.objects.select_for_update().get(pk=campaign.pk)
    if campaign.state == BroadcastCampaign.State.MATERIALIZING:
        if campaign.audience_policy_version != 2:
            raise ValidationError(
                "Cancel the legacy campaign and explicitly confirm a new campaign"
            )
        return campaign
    if campaign.state != BroadcastCampaign.State.DRAFT or not reason.strip():
        raise ValidationError("Only a draft campaign with a reason can be queued")
    if (
        campaign.audience_type in {"USERS", "GROUPS", "BOTH"}
        and not settings.B3_BROADCAST_AUDIENCES_ENABLED
    ):
        raise ValidationError("B3 broadcast audiences are not enabled")
    from apps.notifications.broadcast_content import validate_campaign_source

    validate_campaign_source(campaign)
    estimates = estimated_targets(campaign.audience_type)
    campaign.estimated_users, campaign.estimated_groups = estimates["users"], estimates["groups"]
    campaign.audience_policy_version = 2
    campaign.state = BroadcastCampaign.State.MATERIALIZING
    campaign.queued_at = timezone.now()
    campaign.save(
        update_fields=(
            "state",
            "queued_at",
            "estimated_users",
            "estimated_groups",
            "audience_policy_version",
        )
    )
    OperatorAuditEvent.objects.create(
        actor=actor,
        action="BROADCAST_QUEUE",
        target_type="BroadcastCampaign",
        target_id=str(campaign.public_id),
        reason=reason,
        before_state={"state": "DRAFT"},
        after_state={"state": campaign.state, "audience_policy_version": 2},
    )
    return campaign


def materialize_campaign(campaign_id: int, batch_size: int = 1000) -> int:
    with transaction.atomic():
        campaign = BroadcastCampaign.objects.select_for_update().get(pk=campaign_id)
        if campaign.state != BroadcastCampaign.State.MATERIALIZING:
            return 0
        if campaign.audience_policy_version != 2:
            import structlog

            structlog.get_logger(__name__).warning(
                "broadcast_policy_reconfirmation_required", campaign_id=campaign.pk
            )
            return 0  # Never expand an old in-progress audience without new operator confirmation.
        users = eligible_users().filter(pk__gt=campaign.materialization_cursor).order_by("pk")
        if campaign.audience_type == BroadcastCampaign.Audience.GROUPS:
            users = Player.objects.none()
        batch = list(users[:batch_size])
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
        groups = (
            eligible_groups().filter(pk__gt=campaign.group_materialization_cursor).order_by("pk")
        )
        if campaign.audience_type not in {"GROUPS", "BOTH"}:
            groups = TelegramGroup.objects.none()
        group_batch = list(groups[:batch_size])
        BroadcastRecipient.objects.bulk_create(
            [
                BroadcastRecipient(
                    campaign=campaign,
                    group=g,
                    target_kind="GROUP",
                    telegram_user_id=g.telegram_chat_id,
                )
                for g in group_batch
            ],
            ignore_conflicts=True,
        )
        if group_batch:
            campaign.group_materialization_cursor = group_batch[-1].pk
        if len(batch) < batch_size and len(group_batch) < batch_size:
            campaign.state = BroadcastCampaign.State.QUEUED
        campaign.save(
            update_fields=("materialization_cursor", "group_materialization_cursor", "state")
        )
        count = len(batch) + len(group_batch)
    reconcile_campaign(campaign_id)
    return count


@transaction.atomic
def cancel_campaign(*, campaign: BroadcastCampaign, actor: Any, reason: str) -> BroadcastCampaign:
    if not actor.is_active or not actor.has_perm("notifications.manage_broadcasts"):
        raise PermissionDenied("Broadcast operator authorization is required")
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
        .order_by(F("last_dispatch_at").asc(nulls_first=True), "id")
        .first()
    )
    if campaign is None:
        return []
    campaign.last_dispatch_at = now
    campaign.save(update_fields=("last_dispatch_at",))
    candidates = (
        BroadcastRecipient.objects.select_for_update(skip_locked=True)
        .filter(campaign=campaign)
        .filter(
            Q(state=DeliveryState.PENDING)
            | Q(state=DeliveryState.RETRY_WAIT, next_attempt_at__lte=now)
            | Q(state=DeliveryState.PROCESSING, lease_expires_at__lte=now)
        )
        .order_by("id")
    )
    rows = list(candidates.filter(target_kind="GROUP")[: limit // 2])
    rows += list(candidates.filter(target_kind="USER")[: limit - len(rows)])
    if len(rows) < limit:
        rows += list(candidates.exclude(pk__in=[row.pk for row in rows])[: limit - len(rows)])
    for row in rows:
        if row.state == DeliveryState.PROCESSING:
            row.state = DeliveryState.UNCERTAIN
            row.completed_at = now
            row.error_category = "dispatch_lease_expired"
            row.lease_token = row.lease_expires_at = None
            row.save()
            continue
        row.state = DeliveryState.PROCESSING
        row.lease_token = uuid.uuid4()
        row.lease_expires_at = now + timedelta(seconds=settings.NOTIFICATION_DELIVERY_LEASE_SECONDS)
        row.attempt_count += 1
        row.save(update_fields=("state", "lease_token", "lease_expires_at", "attempt_count"))
    return [row for row in rows if row.state == DeliveryState.PROCESSING]


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


def audience_metrics(campaign: BroadcastCampaign) -> dict[str, Any]:
    def counts(kind: str) -> dict[str, int]:
        values = {
            x["state"]: x["count"]
            for x in campaign.recipients.filter(target_kind=kind)
            .values("state")
            .annotate(count=Count("id"))
        }
        oldest = (
            campaign.recipients.filter(
                target_kind=kind, state__in=[DeliveryState.PENDING, DeliveryState.RETRY_WAIT]
            )
            .order_by("created_at")
            .first()
        )
        return {
            "materialized": sum(values.values()),
            **{state.lower(): values.get(state, 0) for state in DeliveryState.values},
            "oldest_pending_seconds": max(
                0, int((timezone.now() - oldest.created_at).total_seconds())
            )
            if oldest
            else 0,
        }

    return {
        "state": campaign.state,
        "estimated_users": campaign.estimated_users,
        "estimated_groups": campaign.estimated_groups,
        "users": counts("USER"),
        "groups": counts("GROUP"),
    }
