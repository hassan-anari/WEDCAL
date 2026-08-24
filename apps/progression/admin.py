from django.contrib import admin
from django.http import HttpRequest

from apps.progression.models import PlayerProgression


@admin.register(PlayerProgression)
class PlayerProgressionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("player", "lifetime_points", "level", "dexo_stage", "config_version")
    readonly_fields = (
        "player",
        "lifetime_points",
        "level",
        "dexo_stage",
        "config_version",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: PlayerProgression | None = None
    ) -> bool:
        return False
