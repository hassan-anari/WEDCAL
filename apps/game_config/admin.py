from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import preview_safe_cutover, publish_game_config


class PublicationForm(forms.Form):
    reason = forms.CharField(max_length=1000, widget=forms.Textarea)


@admin.register(GameConfigVersion)
class GameConfigVersionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("version", "status", "effective_at", "published_at", "publication_operation")
    actions = None

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<int:object_id>/publish/",
                self.admin_site.admin_view(self.publish_view),
                name="game_config_publish",
            )
        ] + super().get_urls()

    @admin.display(description="Controlled publication")
    def publication_operation(self, obj: GameConfigVersion) -> str:
        if obj.status != GameConfigVersion.Status.DRAFT:
            return "Published"
        return format_html(
            '<a href="{}">Validate / preview / publish</a>',
            reverse("admin:game_config_publish", args=(obj.pk,)),
        )

    def publish_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if not request.user.has_perm("game_config.publish_game_config"):
            raise PermissionDenied
        config = GameConfigVersion.objects.get(pk=object_id)
        issues = preview_safe_cutover(config)
        form = PublicationForm(request.POST or None)
        error = None
        if request.method == "POST" and form.is_valid():
            try:
                publish_game_config(
                    config,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                error = exc
            else:
                return redirect("admin:game_config_gameconfigversion_changelist")
        return TemplateResponse(
            request,
            "admin/game_config/publication_form.html",
            {"form": form, "config": config, "issues": issues, "error": error},
        )

    def get_readonly_fields(
        self, request: HttpRequest, obj: GameConfigVersion | None = None
    ) -> tuple[str, ...]:
        if obj and obj.status == GameConfigVersion.Status.PUBLISHED:
            return (
                "version",
                "schema_version",
                "status",
                "effective_at",
                "payload",
                "payload_checksum",
                "published_at",
                "created_at",
                "updated_at",
            )
        return ("status", "payload_checksum", "published_at", "created_at", "updated_at")

    def has_delete_permission(
        self, request: HttpRequest, obj: GameConfigVersion | None = None
    ) -> bool:
        return bool(obj is None or obj.status == GameConfigVersion.Status.DRAFT)
