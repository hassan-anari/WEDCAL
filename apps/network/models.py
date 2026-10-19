import uuid

from django.db import models
from django.utils import timezone

from apps.game_config.models import GameConfigVersion
from apps.players.models import Player
from apps.rewards.models import RewardLedger
from apps.seasons.models import Season
from apps.telegram.models import TelegramGroup


class CreatorRegistrationIntent(models.Model):
    authorize_group_messages = models.BooleanField(default=False)
    creator = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="network_intents")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CreatorGroupRegistration(models.Model):
    class State(models.TextChoices):
        REVIEW = "REVIEW", "Review"
        ACTIVE = "ACTIVE", "Active"
        ADMIN_REQUIRED = "ADMIN_REQUIRED", "Admin required"
        SUSPENDED = "SUSPENDED", "Suspended"
        INACTIVE = "INACTIVE", "Inactive"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    creator = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="creator_groups")
    group = models.OneToOneField(
        TelegramGroup, on_delete=models.PROTECT, related_name="creator_registration"
    )
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.REVIEW, db_index=True
    )
    bot_verified_at = models.DateTimeField(null=True, blank=True)
    creator_verified_at = models.DateTimeField(null=True, blank=True)
    last_verification_error = models.CharField(max_length=100, blank=True)
    next_verification_at = models.DateTimeField(default=timezone.now, db_index=True)
    reward_eligible_since = models.DateTimeField(null=True, blank=True, db_index=True)
    authorization_intent = models.ForeignKey(
        CreatorRegistrationIntent, null=True, on_delete=models.PROTECT
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CreatorRegistrationStateEvent(models.Model):
    registration = models.ForeignKey(
        CreatorGroupRegistration, on_delete=models.PROTECT, related_name="state_events"
    )
    state = models.CharField(max_length=20, choices=CreatorGroupRegistration.State.choices)
    reward_eligible = models.BooleanField(default=False)
    occurred_at = models.DateTimeField(db_index=True)
    source = models.CharField(max_length=32)
    owner = models.ForeignKey(Player, on_delete=models.PROTECT, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("registration", "occurred_at", "source"),
                name="network_unique_registration_state_event",
            )
        ]


class NetworkActivity(models.Model):
    class Classification(models.TextChoices):
        VALID = "VALID", "Valid"
        SUSPICIOUS = "SUSPICIOUS", "Suspicious"
        REJECTED = "REJECTED", "Rejected"
        CONTROL = "CONTROL", "Control"
        LATE = "LATE", "Late after window finalization"

    registration = models.ForeignKey(
        CreatorGroupRegistration, on_delete=models.PROTECT, related_name="activity"
    )
    group = models.ForeignKey(TelegramGroup, on_delete=models.PROTECT)
    sender = models.ForeignKey(Player, on_delete=models.PROTECT, null=True)
    telegram_message_id = models.BigIntegerField()
    telegram_update_id = models.BigIntegerField()
    observed_at = models.DateTimeField(db_index=True)
    character_count = models.PositiveIntegerField(default=0)
    rewardable_chars = models.PositiveIntegerField(default=0)
    classification = models.CharField(max_length=16, choices=Classification.choices)
    text = models.TextField(null=True, blank=True)
    exact_fingerprint = models.CharField(max_length=64, blank=True)
    normalized_fingerprint = models.CharField(max_length=64, blank=True)
    config_version = models.ForeignKey(GameConfigVersion, on_delete=models.PROTECT)
    season = models.ForeignKey(Season, on_delete=models.PROTECT, null=True)
    program_active = models.BooleanField(default=False)
    window_processed = models.BooleanField(default=False, db_index=True)
    speaker_reward = models.OneToOneField(
        RewardLedger,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="speaker_activity",
    )
    content_expires_at = models.DateTimeField(db_index=True)
    fingerprint_expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("view_network_message_evidence", "Can view Network message evidence")]
        constraints = [
            models.UniqueConstraint(
                fields=("group", "telegram_message_id"), name="network_unique_group_message"
            )
        ]
        indexes = [
            models.Index(fields=("registration", "observed_at"), name="network_registration_time"),
            models.Index(fields=("sender", "observed_at"), name="network_sender_time"),
        ]


class CreatorDailyAllowance(models.Model):
    creator = models.ForeignKey(Player, on_delete=models.PROTECT)
    period_key = models.CharField(max_length=64)
    points = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("creator", "period_key"), name="creator_daily_allowance"
            )
        ]


class CreatorWindowSettlement(models.Model):
    class State(models.TextChoices):
        REWARDED = "REWARDED", "Rewarded"
        ZERO = "ZERO", "Zero reward"
        DEFERRED_SEASON_CLOSE = "DEFERRED_SEASON_CLOSE", "Deferred to Season close"

    registration = models.ForeignKey(
        CreatorGroupRegistration, on_delete=models.PROTECT, related_name="settlements"
    )
    season = models.ForeignKey(Season, on_delete=models.PROTECT)
    config_version = models.ForeignKey(GameConfigVersion, on_delete=models.PROTECT)
    window_start = models.DateTimeField()
    eligible_chars = models.PositiveBigIntegerField(default=0)
    unique_speakers = models.PositiveIntegerField(default=0)
    points = models.PositiveIntegerField(default=0)
    reward = models.OneToOneField(RewardLedger, on_delete=models.PROTECT, null=True, blank=True)
    state = models.CharField(max_length=32, choices=State.choices, default=State.ZERO)
    finalized_at = models.DateTimeField(null=True, blank=True)
    owner = models.ForeignKey(Player, on_delete=models.PROTECT, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("registration", "season", "config_version", "window_start"),
                name="creator_unique_window",
            )
        ]
