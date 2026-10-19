from django.db import models


class PlayerProfile(models.Model):
    player = models.OneToOneField(
        "players.Player", on_delete=models.CASCADE, related_name="collection_profile"
    )
    first_reviewed_at = models.DateTimeField(null=True, blank=True)
    active_frame_key = models.SlugField(max_length=64, default="standard")
    active_title_key = models.SlugField(max_length=64, default="explorer")
    featured_medal_key = models.SlugField(max_length=64, null=True, blank=True)
    featured_achievement_key = models.SlugField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PlayerAchievementUnlock(models.Model):
    player = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="achievement_unlocks"
    )
    achievement_key = models.SlugField(max_length=64)
    unlocked_at = models.DateTimeField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    evidence_category = models.CharField(max_length=64)
    evidence_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "achievement_key"), name="profile_unique_achievement"
            )
        ]


class PlayerMedalAward(models.Model):
    player = models.ForeignKey(
        "players.Player", on_delete=models.PROTECT, related_name="medal_awards"
    )
    medal_key = models.SlugField(max_length=64)
    source_season = models.ForeignKey(
        "seasons.Season", on_delete=models.PROTECT, null=True, blank=True
    )
    awarded_at = models.DateTimeField()
    config_version = models.ForeignKey("game_config.GameConfigVersion", on_delete=models.PROTECT)
    reason = models.CharField(max_length=100)
    evidence_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "medal_key", "source_season"),
                name="profile_unique_medal_award",
                nulls_distinct=False,
            )
        ]
        indexes = [models.Index(fields=("player", "medal_key"), name="profile_player_medal")]
