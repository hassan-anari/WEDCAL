from typing import Any

from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.notifications.models import (
    BroadcastCampaign,
    BroadcastRecipient,
    Notification,
    NotificationTemplate,
    OperatorAuditEvent,
    TelegramDelivery,
)
from apps.notifications.services import campaign_metrics, cancel_campaign, queue_campaign


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("event_key", "channel", "version", "enabled", "created_at")
    readonly_fields = ("created_by", "created_at")

    def get_readonly_fields(
        self, request: HttpRequest, obj: NotificationTemplate | None = None
    ) -> tuple[str, ...]:
        if obj:
            return (
                "event_key",
                "channel",
                "version",
                "title",
                "body",
                "created_by",
                "created_at",
            )
        return ("created_by", "created_at")

    def save_model(
        self, request: HttpRequest, obj: NotificationTemplate, form: Any, change: bool
    ) -> None:
        if not change:
            assert isinstance(request.user, User)
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(BroadcastCampaign)
class BroadcastCampaignAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "public_id",
        "state",
        "audience_type",
        "created_by",
        "created_at",
        "delivery_metrics",
    )
    readonly_fields = (
        "created_by",
        "state",
        "materialization_cursor",
        "group_materialization_cursor",
        "estimated_users",
        "estimated_groups",
        "last_dispatch_at",
        "queued_at",
        "completed_at",
        "cancelled_at",
        "created_at",
    )
    actions = ("queue_selected", "cancel_selected")

    def get_readonly_fields(
        self, request: HttpRequest, obj: BroadcastCampaign | None = None
    ) -> tuple[str, ...]:
        history = (
            "created_by",
            "state",
            "materialization_cursor",
            "group_materialization_cursor",
            "estimated_users",
            "estimated_groups",
            "last_dispatch_at",
            "queued_at",
            "completed_at",
            "cancelled_at",
            "created_at",
        )
        if obj and obj.state != BroadcastCampaign.State.DRAFT:
            return history + ("audience_type", "title_snapshot", "body_snapshot", "purpose")
        return history

    def save_model(
        self, request: HttpRequest, obj: BroadcastCampaign, form: Any, change: bool
    ) -> None:
        if not change:
            assert isinstance(request.user, User)
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def has_module_permission(self, request: HttpRequest) -> bool:
        return request.user.has_perm("notifications.manage_broadcasts")

    def has_view_permission(
        self, request: HttpRequest, obj: BroadcastCampaign | None = None
    ) -> bool:
        return request.user.has_perm("notifications.manage_broadcasts")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return request.user.has_perm("notifications.manage_broadcasts")

    def has_change_permission(
        self, request: HttpRequest, obj: BroadcastCampaign | None = None
    ) -> bool:
        return request.user.has_perm("notifications.manage_broadcasts")

    def has_delete_permission(
        self, request: HttpRequest, obj: BroadcastCampaign | None = None
    ) -> bool:
        # Campaign rows are operational history; cancellation is the only terminal operation.
        return False

    def delivery_metrics(self, obj: BroadcastCampaign) -> dict[str, int]:
        return campaign_metrics(obj)

    @admin.action(description="Queue selected draft campaigns")
    def queue_selected(self, request: HttpRequest, queryset: QuerySet[BroadcastCampaign]) -> None:
        if not request.user.has_perm("notifications.manage_broadcasts"):
            raise PermissionError
        for campaign in queryset:
            queue_campaign(campaign=campaign, actor=request.user, reason=campaign.purpose)
        self.message_user(request, "Campaign queued", messages.SUCCESS)

    @admin.action(description="Cancel selected campaigns")
    def cancel_selected(self, request: HttpRequest, queryset: QuerySet[BroadcastCampaign]) -> None:
        if not request.user.has_perm("notifications.manage_broadcasts"):
            raise PermissionError
        for campaign in queryset:
            cancel_campaign(campaign=campaign, actor=request.user, reason=campaign.purpose)


admin.site.register(Notification, ReadOnlyAdmin)
admin.site.register(TelegramDelivery, ReadOnlyAdmin)
admin.site.register(BroadcastRecipient, ReadOnlyAdmin)
admin.site.register(OperatorAuditEvent, ReadOnlyAdmin)


from apps.notifications.models import (  # noqa: E402
    BotAdminOperator,
    BotAdminSession,
    GroupBroadcastConsentEvent,
    GroupBroadcastSubscription,
)


@admin.register(BotAdminOperator)
class BotAdminOperatorAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("telegram_user_id", "user", "enabled")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return request.user.is_superuser

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return request.user.is_superuser

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(BotAdminSession)
class BotAdminSessionAdmin(ReadOnlyAdmin):
    list_display = ("operator", "step", "expires_at", "campaign")


@admin.register(GroupBroadcastSubscription)
class GroupBroadcastSubscriptionAdmin(ReadOnlyAdmin):
    list_display = ("group", "enabled", "authorized_telegram_user_id", "consented_at", "revoked_at")


@admin.register(GroupBroadcastConsentEvent)
class GroupBroadcastConsentEventAdmin(ReadOnlyAdmin):
    list_display = ("subscription", "actor_telegram_user_id", "enabled", "revision", "created_at")
