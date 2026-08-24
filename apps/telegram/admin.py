from django.contrib import admin

from apps.telegram.models import TelegramGroup, TelegramInboundUpdate, TelegramUserMembership


@admin.register(TelegramInboundUpdate)
class TelegramInboundUpdateAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("update_id", "state", "received_at", "processed_at")
    exclude = ("payload",)
    readonly_fields = ("update_id", "state", "received_at", "processed_at")


@admin.register(TelegramGroup)
class TelegramGroupAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("telegram_chat_id", "title", "bot_membership_status", "is_monitored")
    search_fields = ("telegram_chat_id", "title", "username")


@admin.register(TelegramUserMembership)
class TelegramUserMembershipAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("telegram_user_id", "group", "status", "observed_at")
