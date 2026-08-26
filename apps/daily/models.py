from django.db import models


class DailyClaim(models.Model):
    player = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="daily_claims"
    )
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT)
    claim_period = models.CharField(max_length=64)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    streak_count_after_claim = models.PositiveIntegerField()
    cycle_day = models.PositiveIntegerField()
    reward_amount = models.PositiveBigIntegerField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    reward_ledger = models.OneToOneField(
        "rewards.RewardLedger", on_delete=models.PROTECT, null=True
    )
    claimed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "claim_period"), name="daily_unique_player_period"
            )
        ]
        indexes = [models.Index(fields=("player", "-period_start"), name="daily_player_history")]
