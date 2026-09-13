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
        "queued_at",
        "completed_at",
        "cancelled_at",
        "created_at",
    )
    actions = ("queue_selected", "cancel_selected")

    def save_model(
        self, request: HttpRequest, obj: BroadcastCampaign, form: Any, change: bool
    ) -> None:
        if not change:
            assert isinstance(request.user, User)
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def has_module_permission(self, request: HttpRequest) -> bool:
        return request.user.has_perm("notifications.manage_broadcasts")

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
