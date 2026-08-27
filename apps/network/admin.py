from django.contrib import admin
from django.http import HttpRequest

from apps.network.models import (
    CreatorDailyAllowance,
    CreatorGroupRegistration,
    CreatorRegistrationIntent,
    CreatorWindowSettlement,
    NetworkActivity,
)


class ReadOnlyAuditAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False


@admin.register(CreatorRegistrationIntent)
class CreatorRegistrationIntentAdmin(ReadOnlyAuditAdmin):
    exclude = ("token_hash",)
    list_display = ("id", "creator", "expires_at", "consumed_at")


@admin.register(CreatorGroupRegistration)
class CreatorGroupRegistrationAdmin(ReadOnlyAuditAdmin):
    list_display = ("public_id", "creator", "group", "state", "updated_at")


@admin.register(CreatorDailyAllowance)
class CreatorDailyAllowanceAdmin(ReadOnlyAuditAdmin):
    list_display = ("creator", "period_key", "points")


@admin.register(CreatorWindowSettlement)
class CreatorWindowSettlementAdmin(ReadOnlyAuditAdmin):
    list_display = ("registration", "window_start", "points", "reward")


@admin.register(NetworkActivity)
class NetworkActivityAdmin(ReadOnlyAuditAdmin):
    list_display = (
        "id",
        "registration",
        "sender",
        "observed_at",
        "classification",
        "rewardable_chars",
    )

    def get_exclude(self, request: HttpRequest, obj: object | None = None) -> tuple[str, ...]:
        if request.user.has_perm("network.view_network_message_evidence"):
            return ()
        return ("text",)
