from django.db import models
from django.db.models import Q


class BaseTapDailyAccumulator(models.Model):
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    utc_day = models.DateField()
    accepted_taps = models.PositiveIntegerField(default=0)
    settled_points = models.PositiveBigIntegerField(default=0)
    last_batch_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "season", "utc_day"),
                name="base_tap_daily_source",
            ),
            models.CheckConstraint(
                condition=Q(accepted_taps__gte=0) & Q(settled_points__gte=0),
                name="base_tap_daily_nonnegative",
            ),
        ]


class BaseTapBatch(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SETTLED = "SETTLED", "Settled"

    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    accumulator = models.ForeignKey(
        BaseTapDailyAccumulator, on_delete=models.PROTECT, related_name="batches"
    )
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=100)
    requested_taps = models.PositiveIntegerField()
    accepted_taps = models.PositiveIntegerField()
    rejected_taps = models.PositiveIntegerField()
    earned_points = models.PositiveBigIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    accepted_at = models.DateTimeField()
    settled_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=("status", "accepted_at", "id"))]
        constraints = [
            models.UniqueConstraint(
                fields=("player", "idempotency_key"), name="base_tap_batch_idempotency"
            ),
            models.CheckConstraint(
                condition=Q(requested_taps__gte=1), name="base_tap_requested_positive"
            ),
            models.CheckConstraint(
                condition=Q(requested_taps=models.F("accepted_taps") + models.F("rejected_taps")),
                name="base_tap_batch_counts_balance",
            ),
        ]
