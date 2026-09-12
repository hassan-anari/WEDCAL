from django.contrib import admin

from apps.arena import models

for model in (
    models.RankingProjectionEvent,
    models.RankingProjectionState,
    models.QualificationDecision,
    models.EconomicEpoch,
    models.EpochAnomaly,
    models.EpochPerformanceSnapshot,
    models.EpochCategoryBudget,
    models.GrowthUnlockEvent,
    models.AllocationBatch,
    models.DlxAllocation,
    models.DlxAdjustment,
):
    admin.site.register(model)
