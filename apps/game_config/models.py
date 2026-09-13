from django.core.exceptions import ValidationError
from django.db import models


class GameConfigVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"

    version = models.CharField(max_length=64, unique=True)
    schema_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    effective_at = models.DateTimeField(unique=True)
    payload = models.JSONField()
    payload_checksum = models.CharField(max_length=64, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args: object, **kwargs: object) -> None:
        if self.pk:
            original = GameConfigVersion.objects.get(pk=self.pk)
            if original.status == self.Status.PUBLISHED:
                immutable = (
                    "version",
                    "schema_version",
                    "effective_at",
                    "payload",
                    "payload_checksum",
                    "status",
                )
                if any(getattr(self, field) != getattr(original, field) for field in immutable):
                    raise ValidationError("Published game configurations are immutable")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        if self.status == self.Status.PUBLISHED:
            raise ValidationError("Published game configurations cannot be deleted")
        return super().delete(*args, **kwargs)  # type: ignore[arg-type]

    def __str__(self) -> str:
        return self.version

    class Meta:
        permissions = [("publish_game_config", "Can publish game configuration")]
