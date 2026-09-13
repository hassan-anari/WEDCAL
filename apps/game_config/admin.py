from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import publish_game_config


@admin.register(GameConfigVersion)
class GameConfigVersionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("version", "status", "effective_at", "published_at")
    actions = ("publish_selected",)

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
        return ("payload_checksum", "published_at", "created_at", "updated_at")

    @admin.action(description="Publish selected draft configuration")
    def publish_selected(self, request: HttpRequest, queryset: QuerySet[GameConfigVersion]) -> None:
        if not request.user.has_perm("game_config.publish_game_config"):
            raise PermissionError
        for config in queryset:
            publish_game_config(
                config,
                actor=request.user,
                reason="Django Admin validated publication",
            )
        self.message_user(request, "Configuration published", messages.SUCCESS)

    def has_delete_permission(
        self, request: HttpRequest, obj: GameConfigVersion | None = None
    ) -> bool:
        return bool(obj is None or obj.status == GameConfigVersion.Status.DRAFT)
