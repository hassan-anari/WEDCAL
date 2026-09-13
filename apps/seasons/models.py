import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.game_config.models import GameConfigVersion
from apps.players.models import Player


class Season(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        CLOSED = "CLOSED", "Closed"
        ARCHIVED = "ARCHIVED", "Archived"

    code = models.SlugField(max_length=64, unique=True)
    title = models.CharField(max_length=255)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    baseline_config = models.ForeignKey(
        GameConfigVersion, on_delete=models.PROTECT, related_name="seasons"
    )
    settlement_start_epoch = models.PositiveSmallIntegerField(null=True, blank=True)
    settlement_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")), name="season_end_after_start"
            ),
            models.UniqueConstraint(
                fields=("status",), condition=Q(status="ACTIVE"), name="one_active_season"
            ),
        ]

    def __str__(self) -> str:
        return self.code


class PlayerSeason(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="seasons")
    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="players")
    season_points = models.BigIntegerField(default=0)
    score_reached_at = models.DateTimeField(null=True, blank=True)
    ranking_generation = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("player", "season"), name="unique_player_season"),
            models.CheckConstraint(
                condition=Q(season_points__gte=0), name="player_season_points_nonnegative"
            ),
        ]
        indexes = [models.Index(fields=("season", "-season_points", "score_reached_at", "player"))]


class SeasonSettlementControlEvent(models.Model):
    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="settlement_controls")
    previous_start_epoch = models.PositiveSmallIntegerField()
    new_start_epoch = models.PositiveSmallIntegerField()
    changed_at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    service_actor = models.CharField(max_length=80, blank=True)
    reason = models.CharField(max_length=1000, default="legacy-service-change")
    ticket_reference = models.CharField(max_length=128, blank=True)
    correlation_id = models.UUIDField(null=True)

    class Meta:
        indexes = [models.Index(fields=("season", "changed_at", "id"))]
        permissions = [("change_settlement_control", "Can change settlement control")]


class SeasonCloseRun(models.Model):
    """Durable, resumable coordinator for one Season close operation."""

    class Stage(models.TextChoices):
        PREPARING = "PREPARING", "Preparing"
        DRAINING = "DRAINING", "Draining"
        RECONCILING = "RECONCILING", "Reconciling"
        FREEZING_RANKINGS = "FREEZING_RANKINGS", "Freezing rankings"
        AWARDING_HONORS = "AWARDING_HONORS", "Awarding honors"
        FINALIZING = "FINALIZING", "Finalizing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    season = models.OneToOneField(Season, on_delete=models.PROTECT, related_name="close_run")
    idempotency_key = models.CharField(max_length=200, unique=True)
    stage = models.CharField(max_length=24, choices=Stage.choices, default=Stage.PREPARING)
    cutoff_at = models.DateTimeField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    service_actor = models.CharField(max_length=80, blank=True)
    reason = models.CharField(max_length=1000)
    ticket_reference = models.CharField(max_length=128, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4, unique=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    failure_stage = models.CharField(max_length=24, blank=True)
    failure_category = models.CharField(max_length=100, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    barrier_established_at = models.DateTimeField(null=True, blank=True)
    blocker_kind = models.CharField(max_length=16, blank=True)
    next_recheck_at = models.DateTimeField(null=True, blank=True, db_index=True)
    readiness_evidence = models.JSONField(default=dict)
    drain_evidence = models.JSONField(default=dict)
    reconciliation_evidence = models.JSONField(default=dict)
    progress = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("close_season", "Can preview and close a Season")]
        indexes = [models.Index(fields=("stage", "updated_at"), name="season_close_stage")]


class ImmutableStanding(models.Model):
    final_rank = models.PositiveBigIntegerField()
    score_reached_at = models.DateTimeField()
    ranking_generation = models.PositiveBigIntegerField()
    ranked_population = models.PositiveBigIntegerField()
    provenance = models.JSONField(default=dict)
    finalized_at = models.DateTimeField()

    class Meta:
        abstract = True

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            raise ValidationError("Final standings are immutable")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise ValidationError("Final standings are immutable")


class PlayerSeasonFinalStanding(ImmutableStanding):
    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="final_players")
    player = models.ForeignKey(Player, on_delete=models.PROTECT, related_name="final_standings")
    final_season_points = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("season", "player"), name="season_final_player"),
            models.UniqueConstraint(
                fields=("season", "final_rank"), name="season_final_player_rank"
            ),
            models.CheckConstraint(
                condition=Q(final_rank__gte=1), name="season_player_rank_positive"
            ),
        ]
        indexes = [models.Index(fields=("season", "final_rank"), name="season_final_player_order")]


class ClanSeasonFinalStanding(ImmutableStanding):
    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="final_clans")
    clan_season = models.OneToOneField(
        "clans.ClanSeason", on_delete=models.PROTECT, related_name="final_standing"
    )
    clan = models.ForeignKey("clans.Clan", on_delete=models.PROTECT, related_name="final_standings")
    final_competitive_score = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("season", "final_rank"), name="season_final_clan_rank"),
            models.CheckConstraint(
                condition=Q(final_rank__gte=1), name="season_clan_rank_positive"
            ),
        ]
        indexes = [models.Index(fields=("season", "final_rank"), name="season_final_clan_order")]
