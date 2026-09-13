import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class OperatorAuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80)
    target_id = models.CharField(max_length=128)
    reason = models.CharField(max_length=1000)
    ticket_reference = models.CharField(max_length=128, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    before_state = models.JSONField(default=dict)
    after_state = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class NotificationTemplate(models.Model):
    class Channel(models.TextChoices):
        IN_APP = "IN_APP", "In app"
        TELEGRAM = "TELEGRAM", "Telegram"

    event_key = models.CharField(max_length=80)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    version = models.PositiveIntegerField()
    enabled = models.BooleanField(default=True)
    title = models.CharField(max_length=200, blank=True)
    body = models.TextField(max_length=4000)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event_key", "channel", "version"), name="notification_template_version"
            )
        ]

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            original = NotificationTemplate.objects.get(pk=self.pk)
            if any(
                getattr(original, field) != getattr(self, field)
                for field in ("event_key", "channel", "version", "title", "body")
            ):
                raise ValidationError("Create a new template version instead of rewriting history")
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class Notification(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    recipient = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="notifications"
    )
    event_key = models.CharField(max_length=80)
    source_domain = models.CharField(max_length=64)
    source_type = models.CharField(max_length=64)
    source_id = models.CharField(max_length=128)
    idempotency_key = models.CharField(max_length=200, unique=True)
    title = models.CharField(max_length=200, blank=True)
    body = models.TextField(max_length=4000, blank=True)
    payload = models.JSONField(default=dict)
    template = models.ForeignKey(NotificationTemplate, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=("recipient", "-created_at", "-id"))]


class DeliveryState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    RETRY_WAIT = "RETRY_WAIT", "Retry wait"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    UNCERTAIN = "UNCERTAIN", "Uncertain"
    SKIPPED = "SKIPPED", "Skipped"


class DeliveryFields(models.Model):
    telegram_user_id = models.BigIntegerField()
    state = models.CharField(
        max_length=16, choices=DeliveryState.choices, default=DeliveryState.PENDING
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    telegram_message_id = models.BigIntegerField(null=True)
    error_category = models.CharField(max_length=64, blank=True)
    sent_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class TelegramDelivery(DeliveryFields):
    notification = models.OneToOneField(
        Notification, on_delete=models.PROTECT, related_name="telegram_delivery"
    )
    text_snapshot = models.TextField(max_length=4096, blank=True)


class BroadcastCampaign(models.Model):
    class State(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        MATERIALIZING = "MATERIALIZING", "Materializing"
        QUEUED = "QUEUED", "Queued"
        SENDING = "SENDING", "Sending"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    class Audience(models.TextChoices):
        ALL = "ALL", "All players"
        TELEGRAM_REACHABLE = "TELEGRAM_REACHABLE", "Telegram reachable"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    state = models.CharField(max_length=20, choices=State.choices, default=State.DRAFT)
    audience_type = models.CharField(max_length=24, choices=Audience.choices)
    title_snapshot = models.CharField(max_length=200, blank=True)
    body_snapshot = models.TextField(max_length=4096)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    purpose = models.CharField(max_length=1000)
    materialization_cursor = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    queued_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    cancelled_at = models.DateTimeField(null=True)

    class Meta:
        permissions = [("manage_broadcasts", "Can manage and send broadcasts")]

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            original = BroadcastCampaign.objects.get(pk=self.pk)
            defining_fields = ("audience_type", "title_snapshot", "body_snapshot", "purpose")
            if (original.state != self.State.DRAFT or self.state != self.State.DRAFT) and any(
                getattr(original, field) != getattr(self, field) for field in defining_fields
            ):
                raise ValidationError("Campaign content and audience are immutable after draft")
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class BroadcastRecipient(DeliveryFields):
    campaign = models.ForeignKey(
        BroadcastCampaign, on_delete=models.PROTECT, related_name="recipients"
    )
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("campaign", "player"), name="broadcast_unique_recipient"
            )
        ]
