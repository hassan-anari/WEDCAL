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

    class Meta:
        indexes = [models.Index(fields=("season", "changed_at", "id"))]
