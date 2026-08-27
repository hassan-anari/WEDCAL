from django.db import models
from django.utils import timezone

from apps.game_config.models import GameConfigVersion
from apps.players.models import Player
from apps.seasons.models import Season


class RewardLedger(models.Model):
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="reward_ledger")
    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="reward_ledger")
    source_type = models.CharField(max_length=64)
    source_id = models.CharField(max_length=255)
    context = models.CharField(max_length=64)
    gross_amount = models.BigIntegerField()
    player_amount = models.BigIntegerField()
    clan_treasury_amount = models.BigIntegerField(default=0)
    referral_amount = models.BigIntegerField(default=0)
    config_version = models.ForeignKey(
        GameConfigVersion, on_delete=models.PROTECT, related_name="reward_ledger"
    )
    idempotency_key = models.CharField(max_length=255, unique=True)
    metadata = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("player", "-occurred_at", "-id"), name="reward_player_history"),
            models.Index(fields=("season", "player"), name="reward_season_player"),
            models.Index(fields=("source_type", "source_id"), name="reward_source"),
        ]


class RewardSettlementOutbox(models.Model):
    class State(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        PROCESSED = "PROCESSED", "Processed"
        DEFERRED = "DEFERRED", "Deferred"

    reward = models.OneToOneField(RewardLedger, on_delete=models.CASCADE, related_name="outbox")
    state = models.CharField(
        max_length=16, choices=State.choices, default=State.PENDING, db_index=True
    )
    attempts = models.PositiveIntegerField(default=0)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True, db_index=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    available_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error_category = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
