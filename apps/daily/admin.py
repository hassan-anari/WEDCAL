from django.contrib import admin

from apps.daily.models import DailyClaim


@admin.register(DailyClaim)
class DailyClaimAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "player",
        "season",
        "claim_period",
        "streak_count_after_claim",
        "reward_amount",
        "claimed_at",
    )
    list_filter = ("season", "config_version", "period_start")
    readonly_fields = tuple(field.name for field in DailyClaim._meta.fields)

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False
