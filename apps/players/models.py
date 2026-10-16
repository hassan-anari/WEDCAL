import uuid

from django.db import models


class Player(models.Model):
    class ModerationState(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        FLAGGED = "FLAGGED", "Flagged"
        SUSPENDED = "SUSPENDED", "Suspended"
        BANNED = "BANNED", "Banned"

    class FirstSeenSource(models.TextChoices):
        BOT_START = "BOT_START", "Bot start"
        MINI_APP = "MINI_APP", "Mini App"
        GROUP_ACTIVITY = "GROUP_ACTIVITY", "Group activity"

    telegram_user_id = models.BigIntegerField(unique=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True)
    language_code = models.CharField(max_length=35, blank=True)
    photo_url = models.URLField(max_length=2048, blank=True)
    profile_observed_at = models.DateTimeField(null=True, blank=True)
    profile_last_telegram_update_id = models.BigIntegerField(null=True, blank=True)
    first_seen_at = models.DateTimeField()
    first_seen_source = models.CharField(max_length=20, choices=FirstSeenSource.choices)
    first_private_contact_at = models.DateTimeField(null=True, blank=True)
    first_mini_app_at = models.DateTimeField(null=True, blank=True)
    first_start_parameter = models.CharField(max_length=255, blank=True)
    last_seen_at = models.DateTimeField()
    moderation_state = models.CharField(
        max_length=16, choices=ModerationState.choices, default=ModerationState.ACTIVE
    )
    suspension_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    suspension_action = models.ForeignKey(
        "moderation.ModerationAction",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="active_suspensions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Telegram user {self.telegram_user_id}"


class PlayerAvatar(models.Model):
    """Private object metadata only; photo bytes never enter PostgreSQL."""

    player = models.OneToOneField(Player, on_delete=models.CASCADE)
    delivery_id = models.UUIDField(default=uuid.uuid4, unique=True)
    source_fingerprint = models.CharField(max_length=64)
    object_key = models.CharField(max_length=200, blank=True)
    expires_at = models.DateTimeField(null=True, db_index=True)
    refresh_after = models.DateTimeField(db_index=True)
    refresh_token = models.UUIDField(null=True)
    last_error_category = models.CharField(max_length=40, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class GroupAvatar(models.Model):
    group = models.OneToOneField("telegram.TelegramGroup", on_delete=models.CASCADE)
    delivery_id = models.UUIDField(default=uuid.uuid4, unique=True)
    object_key = models.CharField(max_length=200, blank=True)
    expires_at = models.DateTimeField(null=True, db_index=True)
    refresh_after = models.DateTimeField(db_index=True)
    refresh_token = models.UUIDField(null=True)
    last_error_category = models.CharField(max_length=40, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
