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
        indexes = [
            models.Index(fields=("recipient", "-created_at", "-id")),
            models.Index(fields=("recipient", "-id"), name="notification_feed_page"),
            models.Index(
                fields=("recipient",),
                name="notification_unread",
                condition=models.Q(read_at__isnull=True) & ~models.Q(event_key="BOT_PRIVATE"),
            ),
        ]


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
    source_received_at = models.DateTimeField(null=True)
    text_snapshot = models.TextField(max_length=4096, blank=True)
    message_options = models.JSONField(default=dict)

    def save(self, *args: object, **kwargs: object) -> None:
        creating = self._state.adding
        super().save(*args, **kwargs)  # type: ignore[arg-type]
        if creating and self.state == DeliveryState.PENDING:
            from apps.notifications.dispatch import dispatch_delivery

            dispatch_delivery(self.pk)


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
        USERS = "USERS", "Eligible private users"
        GROUPS = "GROUPS", "Opted-in groups"
        BOTH = "BOTH", "Users and groups"
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
    group_materialization_cursor = models.PositiveBigIntegerField(default=0)
    estimated_users = models.PositiveBigIntegerField(default=0)
    estimated_groups = models.PositiveBigIntegerField(default=0)
    last_dispatch_at = models.DateTimeField(null=True)
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
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT, null=True, blank=True)
    group_subscription = models.ForeignKey(
        "GroupBroadcastSubscription", on_delete=models.PROTECT, null=True, blank=True
    )
    target_kind = models.CharField(
        max_length=8, choices=[("USER", "User"), ("GROUP", "Group")], default="USER"
    )
    subscription_revision = models.PositiveBigIntegerField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("campaign", "player"), name="broadcast_unique_recipient"
            ),
            models.UniqueConstraint(
                fields=("campaign", "group_subscription"), name="broadcast_unique_group"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        target_kind="USER", player__isnull=False, group_subscription__isnull=True
                    )
                    | models.Q(
                        target_kind="GROUP", player__isnull=True, group_subscription__isnull=False
                    )
                ),
                name="broadcast_exact_target",
            ),
        ]


class GroupBroadcastSubscription(models.Model):
    """Explicit consent, independent from Creator rewards and bot observation."""

    group = models.OneToOneField("telegram.TelegramGroup", on_delete=models.PROTECT)
    enabled = models.BooleanField(default=False)
    authorized_telegram_user_id = models.BigIntegerField()
    consented_at = models.DateTimeField(null=True)
    verified_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)
    revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class BotAdminOperator(models.Model):
    telegram_user_id = models.BigIntegerField(unique=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    enabled = models.BooleanField(default=False)


class BotAdminSession(models.Model):
    operator = models.OneToOneField(BotAdminOperator, on_delete=models.PROTECT)
    step = models.CharField(max_length=20, default="MENU")
    campaign = models.ForeignKey(BroadcastCampaign, on_delete=models.PROTECT, null=True)
    expires_at = models.DateTimeField()
    revision = models.PositiveBigIntegerField(default=0)
    last_update_id = models.BigIntegerField(default=-1)


class BotAdminInteraction(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    session = models.ForeignKey(BotAdminSession, on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField()
    action = models.CharField(max_length=32)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True)


class GroupBroadcastConsentEvent(models.Model):
    subscription = models.ForeignKey(GroupBroadcastSubscription, on_delete=models.PROTECT)
    actor_telegram_user_id = models.BigIntegerField()
    enabled = models.BooleanField()
    revision = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("subscription", "revision"), name="group_consent_revision"
            )
        ]


class WelcomeTemplate(models.Model):
    version = models.PositiveIntegerField(unique=True)
    body = models.TextField(max_length=4096)
    keyboard = models.JSONField(
        help_text='{"inline_keyboard":[[{"text":"Play","web_app":{"url":"https://your-app.example/"}}]]}'
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [
            ("activate_welcome", "Can activate or roll back Welcome"),
            ("test_welcome", "Can test Welcome in own authorized operator chat"),
        ]

    def clean(self) -> None:
        from apps.notifications.welcome_config import validate_template

        validate_template(self.body, self.keyboard)

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError("Welcome versions are immutable; create a new version")
        self.full_clean()
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class WelcomeConfiguration(models.Model):
    # Singleton pointer; activation is separate from the disabled-by-default public delivery switch.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    active_template = models.ForeignKey(WelcomeTemplate, null=True, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="welcome_singleton")]
