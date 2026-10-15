import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class BannerAsset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    storage_key = models.CharField(max_length=160, unique=True)
    digest = models.CharField(max_length=64)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    byte_size = models.PositiveIntegerField()
    approved = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class PromotionalBanner(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    placement = models.CharField(max_length=20, choices=[("BASE", "Base")], default="BASE")
    enabled = models.BooleanField(default=False)
    asset = models.ForeignKey(BannerAsset, on_delete=models.PROTECT, null=True, blank=True)
    alt_text = models.CharField(max_length=240)
    destination = models.URLField(max_length=500, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    priority = models.IntegerField(default=0)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("publish_promotionalbanner", "Can publish promotional banners")]
        indexes = [models.Index(fields=("placement", "enabled", "-priority"))]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True)
                | models.Q(starts_at__isnull=True)
                | models.Q(ends_at__gt=models.F("starts_at")),
                name="banner_schedule_valid",
            )
        ]

    def clean(self) -> None:
        from apps.content.services import validate_destination

        validate_destination(self.destination)
        if not self.alt_text.strip():
            raise ValidationError({"alt_text": "An accessibility label is required"})
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError("End must follow start")
        if self.enabled and (
            not self.asset_id or not self.asset or not self.asset.approved or self.deactivated_at
        ):
            raise ValidationError("Publishing requires an approved asset and an active record")
