from django.contrib import admin
from django.http import HttpRequest

from apps.rewards.models import RewardLedger


@admin.register(RewardLedger)
class RewardLedgerAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "player", "season", "source_type", "player_amount", "occurred_at")
    list_filter = ("season", "source_type", "context", "config_version")
    search_fields = ("player__telegram_user_id", "source_id")
    readonly_fields = tuple(field.name for field in RewardLedger._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: RewardLedger | None = None) -> bool:
        return bool(obj is not None and request.method in {"GET", "HEAD"})

    def has_delete_permission(self, request: HttpRequest, obj: RewardLedger | None = None) -> bool:
        return False
