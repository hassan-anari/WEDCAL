from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.utils import timezone


def validate_mission_parameters(family: str, parameters: object) -> None:
    if not isinstance(parameters, dict):
        raise ValidationError({"verification_parameters": "Enter a JSON object."})
    destination = parameters.get("destination_url")
    if family == "TIMED_EXTERNAL" or destination not in (None, ""):
        try:
            if not isinstance(destination, str) or any(
                ord(c) < 33 or c == "\\" for c in destination
            ):
                raise ValidationError("Invalid URL")
            URLValidator(schemes=["http", "https"])(destination)
            parts = urlsplit(destination)
            if parts.username or parts.password or parts.port not in (None, 80, 443):
                raise ValidationError("Invalid URL")
        except (ValidationError, ValueError, TypeError) as exc:
            raise ValidationError(
                {
                    "verification_parameters": (
                        "destination_url must be a full HTTP(S) URL without credentials, "
                        'spaces or custom ports, e.g. {"destination_url":"https://example.com/"}.'
                    )
                }
            ) from exc


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
    activated_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
            ("activate_missiondefinition", "Can activate and deactivate personal missions")
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("key", "version"), name="mission_definition_key_version"
            )
        ]

    def clean(self) -> None:
        validate_mission_parameters(self.verification_family, self.verification_parameters)
        if (
            self.available_from
            and self.available_until
            and self.available_until <= self.available_from
        ):
            raise ValidationError({"available_until": "End must be after start."})
        if self.verification_family == self.Verification.TELEGRAM:
            chat_id = self.verification_parameters.get("telegram_chat_id")
            if type(chat_id) is not int or chat_id == 0:
                raise ValidationError(
                    {"verification_parameters": "telegram_chat_id must be a nonzero integer."}
                )
        if self.pk:
            original = MissionDefinition.objects.get(pk=self.pk)
            if (original.key, original.version) != (self.key, self.version):
                raise ValidationError(
                    "Mission key and version are immutable. Create a new version."
                )
        if self.enabled:
            from apps.game_config.services import resolve_effective_game_config
            from apps.missions.services import MissionUnavailable, _require_policy_binding

            try:
                _require_policy_binding(self, resolve_effective_game_config(timezone.now()))
            except (MissionUnavailable, LookupError, ValueError, ValidationError) as exc:
                raise ValidationError(
                    {
                        "enabled": (
                            "Publish a matching effective GameConfig mission rule before enabling "
                            "this definition (same key, verification family and repeat policy)."
                        )
                    }
                ) from exc
        if self.pk and (
            original.activated_at
            or original.enabled
            or PlayerMission.objects.filter(definition=self).exists()
        ):
            old = MissionDefinition.objects.get(pk=self.pk)
            protected = (
                "key",
                "version",
                "verification_family",
                "repeat_policy",
                "verification_parameters",
                "available_from",
                "available_until",
            )
            if any(getattr(old, field) != getattr(self, field) for field in protected):
                raise ValidationError(
                    "Create a new mission definition version to change verification semantics"
                )

    def save(self, *args: object, **kwargs: object) -> None:
        if self.enabled and self.activated_at is None:
            self.activated_at = timezone.now()
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
