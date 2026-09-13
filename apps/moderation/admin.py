from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.moderation.models import (
    ModerationAction,
    ModerationCase,
    ModerationEvidence,
    ModerationEvidenceAccess,
)


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(ModerationCase)
class ModerationCaseAdmin(ReadOnlyAdmin):
    list_display = ("public_id", "target", "category", "status", "created_at")
    list_filter = ("status", "category")


@admin.register(ModerationEvidence)
class ModerationEvidenceAdmin(ReadOnlyAdmin):
    exclude = ("manual_note",)

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return request.user.has_perm("moderation.view_sensitive_evidence")


admin.site.register(ModerationAction, ReadOnlyAdmin)
admin.site.register(ModerationEvidenceAccess, ReadOnlyAdmin)
