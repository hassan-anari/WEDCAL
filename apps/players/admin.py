from django.contrib import admin

from apps.players.models import Player


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "telegram_user_id", "username", "first_seen_source", "last_seen_at")
    search_fields = ("telegram_user_id", "username")
    readonly_fields = ("first_seen_at", "moderation_state", "created_at", "updated_at")
