from django.contrib import admin

from apps.profile_collection.models import (
    PlayerAchievementUnlock,
    PlayerMedalAward,
    PlayerProfile,
)


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request: object) -> bool:
        return False

    def has_change_permission(self, request: object, obj: object | None = None) -> bool:
        return False

    def has_delete_permission(self, request: object, obj: object | None = None) -> bool:
        return False


admin.site.register(PlayerProfile, ReadOnlyAdmin)
admin.site.register(PlayerAchievementUnlock, ReadOnlyAdmin)
admin.site.register(PlayerMedalAward, ReadOnlyAdmin)
