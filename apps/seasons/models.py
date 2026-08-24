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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("player", "season"), name="unique_player_season"),
            models.CheckConstraint(
                condition=Q(season_points__gte=0), name="player_season_points_nonnegative"
            ),
        ]
