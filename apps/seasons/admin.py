from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.seasons.models import PlayerSeason, Season, SeasonSettlementControlEvent
from apps.seasons.services import activate_season


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("code", "title", "status", "starts_at", "ends_at", "baseline_config")
    actions = ("activate_selected",)

    @admin.action(description="Activate selected draft season")
    def activate_selected(self, request: HttpRequest, queryset: QuerySet[Season]) -> None:
        for season in queryset:
            activate_season(season)

    def get_readonly_fields(
        self, request: HttpRequest, obj: Season | None = None
    ) -> tuple[str, ...]:
        return (
            (
                "status",
                "settlement_start_epoch",
                "settlement_started_at",
                "created_at",
                "updated_at",
            )
            if obj
            else ("status", "created_at", "updated_at")
        )


@admin.register(SeasonSettlementControlEvent)
class SeasonSettlementControlEventAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("season", "previous_start_epoch", "new_start_epoch", "changed_at")
    readonly_fields = ("season", "previous_start_epoch", "new_start_epoch", "changed_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: SeasonSettlementControlEvent | None = None
    ) -> bool:
        return False


@admin.register(PlayerSeason)
class PlayerSeasonAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("player", "season", "season_points", "updated_at")
    readonly_fields = ("player", "season", "season_points", "created_at", "updated_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: PlayerSeason | None = None) -> bool:
        return False
