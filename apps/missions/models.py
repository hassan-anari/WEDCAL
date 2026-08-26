from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models


class MissionDefinition(models.Model):
    class Verification(models.TextChoices):
        INTERNAL = "INTERNAL", "Internal"
        TELEGRAM = "TELEGRAM", "Telegram"
        TIMED_EXTERNAL = "TIMED_EXTERNAL", "Timed external"

    class Repeat(models.TextChoices):
        ONCE = "ONCE_PER_DEFINITION_VERSION", "Once per definition version"
        DAILY = "DAILY", "Daily"

    key = models.SlugField(max_length=100)
    version = models.PositiveIntegerField()
    enabled = models.BooleanField(default=False)
    verification_family = models.CharField(max_length=20, choices=Verification.choices)
    repeat_policy = models.CharField(max_length=32, choices=Repeat.choices)
    available_from = models.DateTimeField(null=True, blank=True)
    available_until = models.DateTimeField(null=True, blank=True)
    verification_parameters = models.JSONField(default=dict, blank=True)
    title = models.CharField(max_length=200, blank=True)
    description = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("key", "version"), name="mission_definition_key_version"
            )
        ]

    def clean(self) -> None:
        if self.verification_family == self.Verification.TIMED_EXTERNAL:
            destination = self.verification_parameters.get("destination_url")
            URLValidator(schemes=["http", "https"])(destination)
        if self.pk and PlayerMission.objects.filter(definition=self).exists():
            old = MissionDefinition.objects.get(pk=self.pk)
            protected = (
                "key",
                "version",
                "verification_family",
                "repeat_policy",
                "verification_parameters",
            )
            if any(getattr(old, field) != getattr(self, field) for field in protected):
                raise ValidationError(
                    "Create a new mission definition version to change verification semantics"
                )

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean()
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class PlayerMission(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        REWARD_READY = "REWARD_READY", "Reward ready"
        CLAIMED = "CLAIMED", "Claimed"

    player = models.ForeignKey("players.Player", on_delete=models.PROTECT)
    definition = models.ForeignKey(
        MissionDefinition, on_delete=models.PROTECT, related_name="progress"
    )
    eligibility_period = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.IN_PROGRESS)
    opened_at = models.DateTimeField(null=True)
    open_token_hash = models.CharField(max_length=64, blank=True)
    season = models.ForeignKey("seasons.Season", on_delete=models.PROTECT, null=True)
    config_version = models.ForeignKey(
        "game_config.GameConfigVersion", on_delete=models.PROTECT, null=True
    )
    reward_amount = models.PositiveIntegerField(null=True)
    verified_at = models.DateTimeField(null=True)
    claimed_at = models.DateTimeField(null=True)
    reward_ledger = models.OneToOneField(
        "rewards.RewardLedger", on_delete=models.PROTECT, null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "definition", "eligibility_period"),
                name="mission_player_eligibility",
            )
        ]
        indexes = [
            models.Index(fields=("player", "status"), name="mission_player_status"),
            models.Index(fields=("status", "verified_at"), name="mission_ready_lookup"),
        ]
