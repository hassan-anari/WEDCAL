import uuid

from django.conf import settings
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
        NOT_YET_QUALIFIED = "NOT_YET_QUALIFIED", "Not yet qualified"
        DISQUALIFIED = "DISQUALIFIED", "Disqualified"
        PENDING = "PENDING", "Pending review"

    player = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="qualification_decisions"
    )
    policy_key = models.CharField(max_length=64)
    policy_version = models.PositiveIntegerField()
    decision = models.CharField(max_length=24, choices=Decision.choices)
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
            models.CheckConstraint(condition=Q(epoch_number__gte=1), name="arena_epoch_range"),
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


class EpochAnomalyResolution(models.Model):
    class Outcome(models.TextChoices):
        NO_ADJUSTMENT_REQUIRED = "NO_ADJUSTMENT_REQUIRED", "No adjustment required"
        ADJUSTMENT_RECORDED = "ADJUSTMENT_RECORDED", "Adjustment recorded"
        ACCEPTED_AS_RECONCILED = "ACCEPTED_AS_RECONCILED", "Accepted as reconciled"

    anomaly = models.OneToOneField(
        EpochAnomaly, on_delete=models.PROTECT, related_name="resolution"
    )
    outcome = models.CharField(max_length=32, choices=Outcome.choices)
    actor = models.ForeignKey("auth.User", on_delete=models.PROTECT, null=True, blank=True)
    service_actor = models.CharField(max_length=80, blank=True)
    reason = models.CharField(max_length=1000)
    evidence_reference = models.CharField(max_length=128, blank=True)
    adjustment = models.ForeignKey(
        "arena.DlxAdjustment", on_delete=models.PROTECT, null=True, blank=True
    )
    idempotency_key = models.CharField(max_length=200, unique=True)
    resolved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("resolve_epoch_anomaly", "Can resolve economic epoch anomaly")]

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            from django.core.exceptions import ValidationError

            raise ValidationError("Epoch anomaly resolutions are append-only")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        from django.core.exceptions import ValidationError

        raise ValidationError("Epoch anomaly resolutions are append-only")


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


class SeasonDlxResidualEvent(models.Model):
    """Append-only disposition of a final category remainder; never a player allocation."""

    season = models.ForeignKey(
        "seasons.Season", on_delete=models.PROTECT, related_name="dlx_residuals"
    )
    category = models.CharField(max_length=40)
    amount = models.PositiveBigIntegerField()
    provenance = models.JSONField(default=dict)
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    disposition = models.CharField(max_length=64)
    reason = models.CharField(max_length=500)
    idempotency_key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("season", "category"), name="arena_season_residual_category"
            )
        ]

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            from django.core.exceptions import ValidationError

            raise ValidationError("Season DLX residual events are append-only")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        from django.core.exceptions import ValidationError

        raise ValidationError("Season DLX residual events are append-only")


class EpochReadinessEvaluation(models.Model):
    """Durable evaluation attempts; terminal evidence is immutable, never a manual override."""

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        BLOCKED = "BLOCKED", "Blocked"
        FAILED = "FAILED", "Failed"
        COMPLETE = "COMPLETE", "Complete"

    epoch = models.ForeignKey(EconomicEpoch, on_delete=models.PROTECT, related_name="evaluations")
    evaluation_key = models.CharField(max_length=200, unique=True)
    policy_key = models.CharField(max_length=64)
    policy_version = models.PositiveIntegerField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    cutoff_at = models.DateTimeField()
    evaluator_version = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    input_digest = models.CharField(max_length=64, blank=True)
    evidence = models.JSONField(default=dict)
    blocking_reasons = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=("epoch", "-id"), name="arena_readiness_latest")]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(status="RUNNING", completed_at__isnull=True)
                    | Q(status__in=["BLOCKED", "FAILED", "COMPLETE"], completed_at__isnull=False)
                ),
                name="arena_readiness_terminal_time",
            ),
        ]


class EconomicPolicyVersion(models.Model):
    """Immutable policy document; existence is not activation authority."""

    key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    payload = models.JSONField()
    checksum = models.CharField(max_length=64)
    approval_reference = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("bind_economic_policy", "Can bind independently approved economic policy")]
        constraints = [
            models.UniqueConstraint(fields=("key", "version"), name="arena_policy_version")
        ]


class EpochPolicyBinding(models.Model):
    epoch = models.OneToOneField(
        EconomicEpoch, on_delete=models.PROTECT, related_name="economic_policy"
    )
    policy = models.ForeignKey(EconomicPolicyVersion, on_delete=models.PROTECT)
    approved_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    evidence_reference = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)


class EconomicRecognition(models.Model):
    """One monetary recognition per canonical source; actor presence is also usable for Loyalty."""

    epoch = models.ForeignKey(EconomicEpoch, on_delete=models.PROTECT, related_name="recognitions")
    player = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="economic_recognitions"
    )
    actor = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="economic_participation"
    )
    category = models.CharField(max_length=40)
    component = models.CharField(max_length=24)
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=128)
    source_time = models.DateTimeField()
    finalized_at = models.DateTimeField()
    recognized_at = models.DateTimeField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    policy = models.ForeignKey(EconomicPolicyVersion, on_delete=models.PROTECT)
    inputs = models.JSONField()
    evidence_digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("source_type", "source_id"), name="arena_recognition_once"
            )
        ]
        indexes = [
            models.Index(fields=("epoch", "category", "player"), name="arena_recognized_category")
        ]


class EconomicDeferral(models.Model):
    """Explicit scoped exposure. This record never asserts that source work completed."""

    epoch = models.ForeignKey(
        EconomicEpoch, on_delete=models.PROTECT, related_name="economic_deferrals"
    )
    provider = models.CharField(max_length=40)
    source_id = models.CharField(max_length=128)
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT, null=True)
    category = models.CharField(max_length=40, blank=True)
    reason = models.CharField(max_length=64)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def assessment(self) -> "CorrectionAssessment":
        row = self.assessments.order_by("-pk").first()
        if row is None:
            raise AttributeError("No economic correction assessment")
        return row

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("epoch", "provider", "source_id"), name="arena_scoped_deferral"
            )
        ]
        permissions = [("resolve_economic_deferral", "Can resolve scoped economic deferral")]


class EconomicDeferralResolution(models.Model):
    deferral = models.OneToOneField(
        EconomicDeferral, on_delete=models.PROTECT, related_name="resolution"
    )
    outcome = models.CharField(max_length=32)
    actor = models.ForeignKey("auth.User", on_delete=models.PROTECT, null=True)
    service_actor = models.CharField(max_length=64, blank=True)
    evidence_reference = models.CharField(max_length=255)
    recognition = models.ForeignKey(EconomicRecognition, on_delete=models.PROTECT, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class EconomicReviewDecision(models.Model):
    epoch = models.ForeignKey(EconomicEpoch, on_delete=models.PROTECT)
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    outcome = models.CharField(
        max_length=16, choices=(("CLEARED", "Cleared"), ("DISQUALIFIED", "Disqualified"))
    )
    actor = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    reason = models.CharField(max_length=1000)
    evidence_reference = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("epoch", "player"), name="arena_review_disposition")
        ]
        permissions = [("review_economic_eligibility", "Can review scoped economic eligibility")]


class EpochSourceManifest(models.Model):
    epoch = models.ForeignKey(
        EconomicEpoch, on_delete=models.PROTECT, related_name="source_manifests"
    )
    policy = models.ForeignKey(EconomicPolicyVersion, on_delete=models.PROTECT)
    input_digest = models.CharField(max_length=64)
    evidence = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("epoch", "input_digest"), name="arena_source_manifest")
        ]


class CategoryCorrectionReserve(models.Model):
    budget = models.OneToOneField(
        EpochCategoryBudget, on_delete=models.PROTECT, related_name="correction_reserve"
    )
    amount = models.PositiveBigIntegerField()
    policy = models.ForeignKey(EconomicPolicyVersion, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class DlxCorrectionEntitlement(models.Model):
    """Reviewed exact entitlement. Approval does not guarantee reserve availability."""

    deferral = models.OneToOneField(
        EconomicDeferral, on_delete=models.PROTECT, related_name="entitlement"
    )
    round = models.ForeignKey(
        "DlxCorrectionRound", on_delete=models.PROTECT, related_name="entitlements"
    )
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    category = models.CharField(max_length=40)
    amount = models.PositiveBigIntegerField()
    actor = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    evidence_reference = models.CharField(max_length=255)
    calculation = models.JSONField()
    idempotency_key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("approve_dlx_correction", "Can approve funded DLX correction entitlement")]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="arena_correction_positive")
        ]


class DlxCorrection(models.Model):
    entitlement = models.OneToOneField(
        DlxCorrectionEntitlement, on_delete=models.PROTECT, related_name="payment"
    )
    reserve = models.ForeignKey(
        CategoryCorrectionReserve, on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="arena_correction_payment_positive"
            )
        ]


class DlxCorrectionAttempt(models.Model):
    entitlement = models.ForeignKey(DlxCorrectionEntitlement, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=200, unique=True)
    outcome = models.CharField(max_length=32)
    available_amount = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class CreatorContentClass(models.Model):
    """Expiring fingerprint lookup; permanent receipts keep only an opaque random class ID."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    epoch = models.ForeignKey(EconomicEpoch, on_delete=models.PROTECT)
    owner = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    day = models.PositiveSmallIntegerField()
    fingerprint = models.CharField(max_length=64, null=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("epoch", "owner", "day", "fingerprint"),
                condition=Q(fingerprint__isnull=False),
                name="arena_creator_content_class",
            )
        ]


class CorrectionAssessment(models.Model):
    deferral = models.ForeignKey(
        EconomicDeferral, on_delete=models.PROTECT, related_name="assessments"
    )
    input_digest = models.CharField(max_length=64, default="")
    performance_units = models.PositiveBigIntegerField()
    creator_h_units = models.PositiveBigIntegerField(default=0)
    actor = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    evidence_reference = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("deferral", "input_digest"), name="arena_assessment_input_unique"
            )
        ]


class DlxCorrectionRound(models.Model):
    reserve = models.OneToOneField(
        CategoryCorrectionReserve, on_delete=models.PROTECT, related_name="correction_round"
    )
    actor = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    input_digest = models.CharField(max_length=64)
    calculation = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class DlxCorrectionAdjustment(models.Model):
    correction = models.ForeignKey(
        DlxCorrection, on_delete=models.PROTECT, related_name="adjustments"
    )
    kind = models.CharField(max_length=16, choices=DlxAdjustment.Kind.choices)
    amount = models.PositiveBigIntegerField()
    actor = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    reason = models.CharField(max_length=1000)
    evidence_reference = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="arena_correction_adjustment_positive"
            )
        ]


class EconomicDeferralEvidence(models.Model):
    """Append-only finalized source proof received after the original scoped case."""

    deferral = models.ForeignKey(EconomicDeferral, on_delete=models.PROTECT, related_name="proofs")
    evidence_digest = models.CharField(max_length=64)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("deferral", "evidence_digest"), name="arena_deferral_proof_unique"
            )
        ]


class GrowthEpochFunding(models.Model):
    """Immutable placement of newly authorized funding; never an allocation."""

    event = models.ForeignKey(GrowthUnlockEvent, on_delete=models.PROTECT, related_name="funding")
    epoch = models.ForeignKey(
        EconomicEpoch, on_delete=models.PROTECT, related_name="growth_funding"
    )
    amount = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("event", "epoch"), name="arena_growth_placement"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="arena_growth_funding_positive"),
        ]


class EconomicAutomationControl(models.Model):
    """Append-only activation/pause authority. No scheduler may create this record."""

    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    enabled = models.BooleanField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    policy = models.ForeignKey("EconomicPolicyVersion", on_delete=models.PROTECT)
    config_checksum = models.CharField(max_length=64)
    capability = models.CharField(max_length=64)
    reason = models.CharField(max_length=1000)
    evidence_reference = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("control_economic_automation", "Can activate or pause DLX automation")]
        indexes = [models.Index(fields=("season", "-id"), name="arena_automation_latest")]


class EconomicAutomationAttempt(models.Model):
    epoch = models.ForeignKey(EconomicEpoch, on_delete=models.PROTECT)
    control = models.ForeignKey(EconomicAutomationControl, on_delete=models.PROTECT)
    stage = models.CharField(max_length=32)
    status = models.CharField(max_length=16, default="RUNNING")
    error_code = models.CharField(max_length=128, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)
    evidence = models.JSONField(default=dict)

    class Meta:
        indexes = [models.Index(fields=("status", "started_at"), name="arena_automation_status")]


class GrowthEvaluation(models.Model):
    """Append-only evaluation/retry evidence, separate from monetary milestone authorization."""

    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    epoch = models.ForeignKey(EconomicEpoch, on_delete=models.PROTECT)
    policy = models.ForeignKey(EconomicPolicyVersion, on_delete=models.PROTECT)
    cutoff_at = models.DateTimeField()
    qualified_player_count = models.PositiveBigIntegerField()
    cohort_digest = models.CharField(max_length=64)
    config_checksum = models.CharField(max_length=64)
    outcome = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=("season", "epoch", "-id"), name="arena_growth_evaluation")]
        permissions = [("view_economic_overview", "Can inspect read-only economic overviews")]


class EconomicPolicyProposal(models.Model):
    key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    payload = models.JSONField()
    checksum = models.CharField(max_length=64)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    submission_reference = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [
            ("propose_economic_policy", "Can submit validated economic policy proposals")
        ]
        constraints = [
            models.UniqueConstraint(fields=("key", "version"), name="arena_policy_proposal_version")
        ]


class EconomicPolicyApproval(models.Model):
    proposal = models.OneToOneField(EconomicPolicyProposal, on_delete=models.PROTECT)
    policy = models.OneToOneField(EconomicPolicyVersion, on_delete=models.PROTECT)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    approval_reference = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("approve_economic_policy", "Can independently approve economic policies")]


class EconomicAlert(models.Model):
    fingerprint = models.CharField(max_length=64, unique=True)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    epoch = models.ForeignKey(EconomicEpoch, null=True, on_delete=models.PROTECT)
    code = models.CharField(max_length=48)
    severity = models.CharField(max_length=16)
    status = models.CharField(max_length=16, default="OPEN")
    evidence = models.JSONField(default=dict)
    opened_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=("status", "season"), name="arena_alert_open")]
        permissions = [
            ("receive_economic_alert", "Can receive private economic alerts"),
            ("test_economic_alert", "Can test economic alerts in own authorized private chat"),
        ]


class EconomicAlertEvent(models.Model):
    alert = models.ForeignKey(EconomicAlert, on_delete=models.PROTECT, related_name="events")
    outcome = models.CharField(max_length=16)
    severity = models.CharField(max_length=16)
    evidence = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class EconomicAlertDelivery(models.Model):
    # LEGACY preserves earlier delivery history without retaining an executable transport.
    channel = models.CharField(max_length=24, default="LEGACY")
    operator = models.ForeignKey(
        "notifications.BotAdminOperator", null=True, on_delete=models.PROTECT
    )
    telegram_user_id = models.BigIntegerField(null=True)
    telegram_message_id = models.BigIntegerField(null=True)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    event = models.ForeignKey(EconomicAlertEvent, on_delete=models.PROTECT)
    route_key = models.CharField(max_length=64)
    route_config = models.JSONField()
    status = models.CharField(max_length=16, default="PENDING")
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    delivered_at = models.DateTimeField(null=True)
    last_error_code = models.CharField(max_length=48, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("event", "route_key"), name="arena_alert_route_once"),
            models.UniqueConstraint(fields=("event", "operator"), name="arena_alert_operator_once"),
            models.UniqueConstraint(
                fields=("event", "telegram_user_id"), name="arena_alert_private_chat_once"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    channel="LEGACY", operator__isnull=True, telegram_user_id__isnull=True
                )
                | models.Q(
                    channel="TELEGRAM_PRIVATE",
                    operator__isnull=False,
                    telegram_user_id__isnull=False,
                    telegram_user_id__gt=0,
                ),
                name="arena_alert_private_target",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "next_attempt_at"), name="arena_alert_due"),
            models.Index(fields=("status", "lease_expires_at"), name="arena_alert_send_lease"),
        ]
