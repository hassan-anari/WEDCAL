import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone


class BaseTapDailyAccumulator(models.Model):
    class SettlementState(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        SETTLED = "SETTLED", "Settled"
        RETRY = "RETRY", "Retry"

    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    utc_day = models.DateField()
    accepted_taps = models.PositiveIntegerField(default=0)
    pending_points = models.PositiveBigIntegerField(default=0)
    settled_points = models.PositiveBigIntegerField(default=0)
    settlement_state = models.CharField(
        max_length=12, choices=SettlementState.choices, default=SettlementState.PENDING
    )
    settlement_attempts = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error_category = models.CharField(max_length=100, blank=True)
    first_accepted_at = models.DateTimeField(null=True)
    last_batch_at = models.DateTimeField(null=True)
    settled_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("settlement_state", "next_attempt_at", "utc_day"))]
        constraints = [
            models.UniqueConstraint(
                fields=("player", "season", "utc_day"), name="base_tap_daily_source"
            ),
            models.CheckConstraint(
                condition=Q(accepted_taps__gte=0)
                & Q(pending_points__gte=0)
                & Q(settled_points__gte=0),
                name="base_tap_daily_nonnegative",
            ),
        ]


class BaseTapReceipt(models.Model):
    """Short-lived idempotency receipt; only economically accepted batches are persisted."""

    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    accumulator = models.ForeignKey(
        BaseTapDailyAccumulator, on_delete=models.CASCADE, related_name="receipts"
    )
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=100)
    requested_taps = models.PositiveIntegerField()
    accepted_taps = models.PositiveIntegerField()
    rejected_taps = models.PositiveIntegerField()
    earned_points = models.PositiveBigIntegerField()
    accepted_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "idempotency_key"), name="base_tap_receipt_idempotency"
            ),
            models.CheckConstraint(
                condition=Q(accepted_taps__gte=1), name="base_tap_receipt_accepted_positive"
            ),
            models.CheckConstraint(
                condition=Q(requested_taps=models.F("accepted_taps") + models.F("rejected_taps")),
                name="base_tap_receipt_counts_balance",
            ),
        ]


class TapBurstState(models.Model):
    """One allowance across every device; expiry/takeover never resets remaining taps."""

    player = models.OneToOneField("players.Player", on_delete=models.PROTECT)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    generation = models.PositiveBigIntegerField(default=0)
    revision = models.PositiveBigIntegerField(default=0)
    remaining = models.PositiveIntegerField(default=0)
    next_sequence = models.PositiveIntegerField(default=1)
    cooldown_until = models.DateTimeField(null=True)
    last_batch_at = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(next_sequence__gte=1), name="tap_sequence_positive")
        ]


class TapLease(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    session_id = models.UUIDField()
    generation = models.PositiveBigIntegerField()
    allowance = models.PositiveIntegerField()
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True)
    delete_after = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("player", "generation"), name="tap_lease_generation")
        ]
