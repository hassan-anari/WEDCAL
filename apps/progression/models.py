from django.db import models
from django.db.models import Q

from apps.game_config.models import GameConfigVersion
from apps.players.models import Player


class PlayerProgression(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name="progression")
    lifetime_points = models.BigIntegerField(default=0)
    level = models.PositiveIntegerField(default=1)
    dexo_stage = models.CharField(max_length=64)
    config_version = models.ForeignKey(
        GameConfigVersion, on_delete=models.PROTECT, related_name="progression_projections"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(lifetime_points__gte=0), name="progression_lifetime_nonnegative"
            )
        ]
