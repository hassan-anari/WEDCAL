from django.db import models
from django.db.models import Q


class RankingProjectionEvent(models.Model):
    class Entity(models.TextChoices):
        PLAYER = "PLAYER", "Player"
        CLAN = "CLAN", "Clan"

    class Operation(models.TextChoices):
        UPSERT = "UPSERT", "Upsert"
        REMOVE = "REMOVE", "Remove"

    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    entity_type = models.CharField(max_length=8, choices=Entity.choices)
    entity_id = models.PositiveBigIntegerField()
    score = models.BigIntegerField()
    score_reached_at = models.DateTimeField()
    operation = models.CharField(max_length=8, choices=Operation.choices)
    source_id = models.CharField(max_length=200)
    generation = models.PositiveBigIntegerField(default=0)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("entity_type", "source_id"), name="arena_rank_event_source"
            )
        ]
        indexes = [models.Index(fields=("season", "entity_type", "id"), name="arena_rank_replay")]


class RankingProjectionState(models.Model):
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    entity_type = models.CharField(max_length=8, choices=RankingProjectionEvent.Entity.choices)
    high_watermark = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("season", "entity_type"), name="arena_rank_state")
        ]


class QualificationDecision(models.Model):
    class Decision(models.TextChoices):
        QUALIFIED = "QUALIFIED", "Qualified"
        DISQUALIFIED = "DISQUALIFIED", "Disqualified"
        PENDING = "PENDING", "Pending review"

    player = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="qualification_decisions"
    )
    policy_key = models.CharField(max_length=64)
    policy_version = models.PositiveIntegerField()
    decision = models.CharField(max_length=16, choices=Decision.choices)
    effective_at = models.DateTimeField()
    reason_code = models.CharField(max_length=64)
    source = models.CharField(max_length=64)
    source_id = models.CharField(max_length=128)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("policy_key", "source", "source_id"), name="arena_qualification_source"
            )
        ]
        indexes = [
            models.Index(
                fields=("player", "policy_key", "policy_version", "-effective_at", "-id"),
                name="arena_qualification_current",
            )
        ]


class EconomicEpoch(models.Model):
    class State(models.TextChoices):
        OPEN = "OPEN", "Open"
        DRAINING = "DRAINING", "Draining"
        FROZEN = "FROZEN", "Frozen"
        SETTLING = "SETTLING", "Settling"
        SETTLED = "SETTLED", "Settled"

    season = models.ForeignKey(
        "seasons.Season", on_delete=models.PROTECT, related_name="economic_epochs"
    )
    epoch_number = models.PositiveSmallIntegerField()
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    base_budget = models.PositiveBigIntegerField()
    growth_budget = models.PositiveBigIntegerField(default=0)
    freeze_watermark = models.JSONField(default=dict)
    frozen_at = models.DateTimeField(null=True)
    snapshots_completed_at = models.DateTimeField(null=True)
    settlement_started_at = models.DateTimeField(null=True)
    settlement_completed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("season", "epoch_number"), name="arena_epoch_number"),
            models.CheckConstraint(
                condition=Q(epoch_number__gte=1, epoch_number__lte=6), name="arena_epoch_range"
            ),
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")), name="arena_epoch_time"
            ),
        ]
        indexes = [
            models.Index(fields=("season", "state", "epoch_number"), name="arena_epoch_state")
        ]


class EpochAnomaly(models.Model):
    epoch = models.ForeignKey(EconomicEpoch, on_delete=models.PROTECT, related_name="anomalies")
    provider = models.CharField(max_length=64)
    source_id = models.CharField(max_length=128)
    source_time = models.DateTimeField()
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("epoch", "provider", "source_id"), name="arena_epoch_anomaly"
            )
        ]


class EpochPerformanceSnapshot(models.Model):
    epoch = models.ForeignKey(
        EconomicEpoch, on_delete=models.PROTECT, related_name="performance_snapshots"
    )
    category = models.CharField(max_length=40)
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    raw_inputs = models.JSONField(default=dict)
    performance_units = models.PositiveBigIntegerField(default=0)
    qualification_decision = models.ForeignKey(QualificationDecision, on_delete=models.PROTECT)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("epoch", "category", "player"), name="arena_epoch_player_category"
            )
        ]
        indexes = [models.Index(fields=("epoch", "category"), name="arena_snapshot_category")]


class PerformanceSourceFact(models.Model):
    """Permanent source-time input for aggregates whose operational receipts may expire."""

    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    category = models.CharField(max_length=40)
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=128)
    source_time = models.DateTimeField()
    amount = models.PositiveBigIntegerField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("source_type", "source_id"), name="arena_performance_source"
            )
        ]
        indexes = [
            models.Index(fields=("season", "source_time", "category"), name="arena_source_epoch")
        ]


class EpochCategoryBudget(models.Model):
    epoch = models.ForeignKey(
        EconomicEpoch, on_delete=models.PROTECT, related_name="category_budgets"
    )
    category = models.CharField(max_length=40)
    weight_bps = models.PositiveSmallIntegerField()
    base_epoch_budget = models.PositiveBigIntegerField()
    growth_contribution = models.PositiveBigIntegerField(default=0)
    carry_in = models.PositiveBigIntegerField(default=0)
    carry_materialized = models.BooleanField(default=False)
    effective_budget = models.PositiveBigIntegerField()
    allocated_amount = models.PositiveBigIntegerField(default=0)
    carry_out = models.PositiveBigIntegerField(default=0)
    residual_amount = models.PositiveBigIntegerField(default=0)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("epoch", "category"), name="arena_epoch_category_budget"
            )
        ]


class GrowthUnlockEvent(models.Model):
    season = models.ForeignKey(
        "seasons.Season", on_delete=models.PROTECT, related_name="growth_unlocks"
    )
    milestone_key = models.CharField(max_length=64)
    qualified_player_count = models.PositiveBigIntegerField()
    unlocked_amount = models.PositiveBigIntegerField()
    policy_key = models.CharField(max_length=64)
    policy_version = models.PositiveIntegerField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("season", "milestone_key"), name="arena_growth_milestone"
            )
        ]


class AllocationBatch(models.Model):
    epoch = models.OneToOneField(
        EconomicEpoch, on_delete=models.PROTECT, related_name="allocation_batch"
    )
    idempotency_key = models.CharField(max_length=200, unique=True)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class DlxAllocation(models.Model):
    player = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="dlx_allocations"
    )
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    epoch = models.ForeignKey(EconomicEpoch, on_delete=models.PROTECT)
    category = models.CharField(max_length=40)
    amount = models.PositiveBigIntegerField()
    snapshot = models.ForeignKey(EpochPerformanceSnapshot, on_delete=models.PROTECT)
    batch = models.ForeignKey(AllocationBatch, on_delete=models.PROTECT, related_name="allocations")
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=200, unique=True)
    metadata = models.JSONField(default=dict)
    confirmed_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=("player", "season"), name="arena_allocation_player"),
            models.Index(fields=("epoch", "category"), name="arena_allocation_category"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="arena_allocation_positive"),
            models.UniqueConstraint(fields=("batch", "snapshot"), name="arena_allocation_snapshot"),
        ]


class DlxAdjustment(models.Model):
    class Kind(models.TextChoices):
        HOLD = "HOLD", "Hold"
        RELEASE_HOLD = "RELEASE_HOLD", "Release hold"
        FORFEIT = "FORFEIT", "Forfeit"
        REVERSAL = "REVERSAL", "Reversal"

    allocation = models.ForeignKey(
        DlxAllocation, on_delete=models.PROTECT, related_name="adjustments"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    amount = models.PositiveBigIntegerField()
    reason_code = models.CharField(max_length=64)
    evidence_reference = models.CharField(max_length=128, blank=True)
    actor_player = models.ForeignKey(
        "players.Player",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dlx_adjustments_made",
    )
    system_actor = models.CharField(max_length=64, blank=True)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=200, unique=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="arena_adjustment_positive")
        ]
        indexes = [
            models.Index(fields=("allocation", "created_at"), name="arena_adjustment_lookup")
        ]
