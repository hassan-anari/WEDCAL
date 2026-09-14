from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.arena import models


class ReadOnlyEconomicAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


for model in (
    models.RankingProjectionEvent,
    models.RankingProjectionState,
    models.QualificationDecision,
    models.EconomicEpoch,
    models.EpochAnomaly,
    models.EpochAnomalyResolution,
    models.EpochPerformanceSnapshot,
    models.EpochCategoryBudget,
    models.GrowthUnlockEvent,
    models.AllocationBatch,
    models.DlxAllocation,
    models.DlxAdjustment,
    models.SeasonDlxResidualEvent,
):
    admin.site.register(model, ReadOnlyEconomicAdmin)
