from typing import Any

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from django.utils.html import format_html

from apps.content import storage
from apps.content.models import BannerAsset, PromotionalBanner
from apps.content.services import encode_image, save_banner, upload_asset
from apps.notifications.admin import ReadOnlyAdmin


class BannerForm(forms.ModelForm):  # type: ignore[type-arg]
    image_upload = forms.FileField(
        required=False,
        help_text="1200 × 260 recommended. Static JPEG/PNG/WebP only; re-encoded as WebP.",
    )

    def clean_image_upload(self) -> Any:
        upload = self.cleaned_data.get("image_upload")
        if upload:
            if not storage.configured():
                raise ValidationError("Banner storage is not configured")
            encode_image(upload)
            upload.seek(0)
        return upload

    def _post_clean(self) -> None:
        enabled = self.cleaned_data.get("enabled", False)
        uploading = bool(self.cleaned_data.get("image_upload"))
        if uploading:
            self.cleaned_data["enabled"] = False
        try:
            super()._post_clean()  # type: ignore[misc]
        finally:
            if uploading:
                self.cleaned_data["enabled"] = enabled
                self.instance.enabled = enabled

    class Meta:
        model = PromotionalBanner
        exclude = ("operator",)


@admin.register(BannerAsset)
class BannerAssetAdmin(ReadOnlyAdmin):
    list_display = ("id", "width", "height", "byte_size", "approved", "created_at")


@admin.register(PromotionalBanner)
class BannerAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    form = BannerForm
    list_display = ("id", "placement", "enabled", "priority", "starts_at", "ends_at", "preview")
    readonly_fields = ("operator", "created_at", "updated_at", "preview", "audit_history")
    actions = ("enable_selected", "disable_selected")

    def preview(self, obj: PromotionalBanner) -> str:
        from django.conf import settings

        if not obj.asset_id or not obj.asset:
            return "No image"
        return format_html(
            '<img src="{}/api/v1/content/banner-assets/{}/{}.webp" width="360" alt="{}">',
            settings.BANNER_ASSET_ORIGIN.rstrip("/"),
            obj.asset_id,
            obj.asset.digest,
            obj.alt_text,
        )

    def audit_history(self, obj: PromotionalBanner) -> str:
        return format_html(
            '<a href="/admin/notifications/operatorauditevent/?target_id={}">Audit history</a>',
            obj.pk,
        )

    def save_model(
        self, request: HttpRequest, obj: PromotionalBanner, form: Any, change: bool
    ) -> None:
        if form.cleaned_data.get("image_upload"):
            obj.asset = upload_asset(form.cleaned_data["image_upload"], request.user)
        save_banner(obj, request.user)

    def delete_model(self, request: HttpRequest, obj: PromotionalBanner) -> None:
        save_banner(obj, request.user, deactivate=True)

    def delete_queryset(self, request: HttpRequest, queryset: Any) -> None:
        for obj in queryset:
            self.delete_model(request, obj)

    @admin.action(description="Enable banners (publish permission required)")
    def enable_selected(self, request: HttpRequest, queryset: Any) -> None:
        for obj in queryset:
            obj.enabled = True
            save_banner(obj, request.user)

    @admin.action(description="Disable banners (publish permission required)")
    def disable_selected(self, request: HttpRequest, queryset: Any) -> None:
        for obj in queryset:
            obj.enabled = False
            save_banner(obj, request.user)
