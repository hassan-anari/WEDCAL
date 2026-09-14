from django.contrib import admin

from apps.telegram.models import (
    TelegramDeadLetterDisposition,
    TelegramGroup,
    TelegramInboundUpdate,
    TelegramUserMembership,
)


@admin.register(TelegramInboundUpdate)
class TelegramInboundUpdateAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("update_id", "state", "received_at", "processed_at")
    exclude = ("payload",)
    readonly_fields = ("update_id", "state", "received_at", "processed_at")

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False


@admin.register(TelegramDeadLetterDisposition)
class TelegramDeadLetterDispositionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("inbound_update", "outcome", "actor", "service_actor", "created_at")

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False


@admin.register(TelegramGroup)
class TelegramGroupAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("telegram_chat_id", "title", "bot_membership_status", "is_monitored")
    search_fields = ("telegram_chat_id", "title", "username")


@admin.register(TelegramUserMembership)
class TelegramUserMembershipAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("telegram_user_id", "group", "status", "observed_at")
