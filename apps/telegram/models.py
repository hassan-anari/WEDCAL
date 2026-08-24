from django.db import models


class TelegramInboundUpdate(models.Model):
    class State(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSED = "PROCESSED", "Processed"
        IGNORED = "IGNORED", "Ignored"
        FAILED = "FAILED", "Failed"

    update_id = models.BigIntegerField(unique=True)
    payload = models.JSONField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    received_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    dispatch_attempts = models.PositiveIntegerField(default=0)
    processing_attempts = models.PositiveIntegerField(default=0)
    last_error_category = models.CharField(max_length=100, blank=True)
    payload_expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("state", "received_at"))]


class TelegramGroup(models.Model):
    class ChatType(models.TextChoices):
        GROUP = "group", "Group"
        SUPERGROUP = "supergroup", "Supergroup"

    telegram_chat_id = models.BigIntegerField(unique=True)
    chat_type = models.CharField(max_length=16, choices=ChatType.choices)
    title = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True)
    bot_membership_status = models.CharField(max_length=32, blank=True)
    bot_is_admin = models.BooleanField(default=False)
    is_monitored = models.BooleanField(default=False, db_index=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.title or str(self.telegram_chat_id)


class TelegramUserMembership(models.Model):
    group = models.ForeignKey(TelegramGroup, on_delete=models.CASCADE, related_name="memberships")
    telegram_user_id = models.BigIntegerField()
    player = models.ForeignKey(
        "players.Player",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="telegram_memberships",
    )
    status = models.CharField(max_length=32)
    is_admin = models.BooleanField(default=False)
    is_owner = models.BooleanField(default=False)
    observed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("group", "telegram_user_id"), name="telegram_unique_group_user"
            )
        ]
        indexes = [models.Index(fields=("telegram_user_id", "status"))]
