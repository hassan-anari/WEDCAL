"""Read-only bounded economic reports. No projection here grants economic authority."""

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Count, Sum

from apps.arena import models
from apps.arena.epochs import reconcile_epoch
from apps.arena.growth import growth_cohort_queryset
from apps.arena.policy import CATEGORIES
from apps.game_config.services import resolve_game_config_version
from apps.seasons.close import reconcile_season_dlx
from apps.seasons.models import Season


def require_auditor(user: Any) -> None:
    if not user.is_active or not user.is_staff or not user.has_perm("arena.view_economic_overview"):
        raise PermissionDenied("Economic auditor permission required")


def total(queryset: Any, field: str = "amount") -> int:
    return int(queryset.aggregate(value=Sum(field))["value"] or 0)


def season_report(season: Season, *, after_epoch: int = 0) -> dict[str, Any]:
    setup = resolve_game_config_version(season.baseline_config_id).dlx_distribution
    if setup is None:
        return {"season": season.code, "status": "SETUP_UNAVAILABLE"}
    growth = total(season.growth_unlocks.all(), "unlocked_amount")
    latest = (
        season.economic_epochs.filter(source_manifests__isnull=False)
        .order_by("-epoch_number")
        .first()
    )
    latest_event = season.growth_unlocks.order_by("-pk").first()
    count = None
    if latest is not None and hasattr(latest, "economic_policy"):
        count = growth_cohort_queryset(latest).count()
    events = {row.milestone_key: row for row in season.growth_unlocks.all()}
    correction_reserves = models.CategoryCorrectionReserve.objects.filter(
        budget__epoch__season=season
    )
    budget_rows = models.EpochCategoryBudget.objects.filter(epoch__season=season)
    reconciliation = reconcile_season_dlx(season)
    reconciliation.pop("epoch_states", None)
    return {
        "season": season.code,
        "status": season.status,
        "config_id": season.baseline_config_id,
        "config_checksum": season.baseline_config.payload_checksum,
        "base_budget": setup.base_pool_dlx_amount,
        "maximum_budget": setup.max_pool_dlx_amount,
        "authorized_growth": growth,
        "authorized_total": setup.base_pool_dlx_amount + growth,
        "funding_placements_total": total(
            models.GrowthEpochFunding.objects.filter(event__season=season)
        ),
        "distinct_qualified_players": count,
        "qualification_cutoff": latest.ends_at if latest else None,
        "qualification_status": "FINALIZED"
        if latest and latest.state in {"FROZEN", "SETTLING", "SETTLED"}
        else "PREPARED_REQUIRES_CURRENT_READINESS"
        if latest
        else "NOT_PREPARED",
        "last_committed_growth_count": latest_event.qualified_player_count
        if latest_event
        else None,
        "milestones": [
            {
                "key": item.key,
                "threshold": item.qualified_player_count,
                "configured_amount": item.unlock_dlx_amount,
                "status": events[item.key].evidence.get("outcome", "LEGACY_EVIDENCE")
                if item.key in events
                else "NOT_AUTHORIZED",
                "event_id": events[item.key].pk if item.key in events else None,
                "authorized_amount": events[item.key].unlocked_amount if item.key in events else 0,
            }
            for item in setup.growth_milestones[:100]
        ],
        "milestone_count": len(setup.growth_milestones),
        "epochs": list(
            season.economic_epochs.filter(epoch_number__gt=after_epoch)
            .order_by("epoch_number")
            .values(
                "id",
                "epoch_number",
                "state",
                "base_budget",
                "growth_budget",
                "frozen_at",
                "settlement_completed_at",
            )[:100]
        ),
        "confirmed_allocations": total(models.DlxAllocation.objects.filter(season=season)),
        "category_carry": list(
            budget_rows.filter(epoch__state="SETTLED")
            .order_by("category", "-epoch__epoch_number")
            .distinct("category")
            .values("category", "epoch__epoch_number", "carry_out")
        ),
        "category_carry_historical_flows": list(
            budget_rows.values("category").annotate(
                carry_in_total=Sum("carry_in"), carry_out_total=Sum("carry_out")
            )
        ),
        "correction_reserve_funding": total(correction_reserves),
        "correction_payments": total(
            models.DlxCorrection.objects.filter(reserve__in=correction_reserves)
        ),
        "outstanding_deferrals": models.EconomicDeferral.objects.filter(
            epoch__season=season, resolution__isnull=True
        ).count(),
        "pending_review_cases": models.EconomicDeferral.objects.filter(
            epoch__season=season, provider="PLAYER_REVIEW", resolution__isnull=True
        ).count(),
        "reconciliation": reconciliation,
        "final_residual": total(season.dlx_residuals.all())
        if season.dlx_residuals.exists()
        else None,
    }


def epoch_report(epoch: models.EconomicEpoch) -> dict[str, Any]:
    binding = models.EpochPolicyBinding.objects.select_related("policy").filter(epoch=epoch).first()
    readiness = epoch.evaluations.order_by("-pk").first()
    manifest = epoch.source_manifests.order_by("-pk").first()
    reserve = models.CategoryCorrectionReserve.objects.filter(budget__epoch=epoch)
    return {
        "epoch_id": epoch.pk,
        "season": epoch.season.code,
        "number": epoch.epoch_number,
        "state": epoch.state,
        "starts_at": epoch.starts_at,
        "ends_at": epoch.ends_at,
        "config_id": epoch.config_version_id,
        "policy_id": binding.policy_id if binding else None,
        "policy_checksum": binding.policy.checksum if binding else None,
        "manifest_id": manifest.pk if manifest else None,
        "manifest_digest": manifest.input_digest if manifest else None,
        "readiness": readiness.status if readiness else "NOT_EVALUATED",
        "readiness_blockers": readiness.blocking_reasons if readiness else None,
        "base_budget": epoch.base_budget,
        "growth": epoch.growth_budget,
        "categories": list(
            epoch.category_budgets.order_by("category").values(
                "category",
                "base_epoch_budget",
                "growth_contribution",
                "carry_in",
                "carry_out",
                "effective_budget",
                "allocated_amount",
                "residual_amount",
            )
        ),
        "frozen_at": epoch.frozen_at,
        "snapshots_completed_at": epoch.snapshots_completed_at,
        "settlement_started_at": epoch.settlement_started_at,
        "settlement_completed_at": epoch.settlement_completed_at,
        "confirmed_allocations": total(models.DlxAllocation.objects.filter(epoch=epoch)),
        "correction_reserve_funding": total(reserve),
        "correction_payments": total(models.DlxCorrection.objects.filter(reserve__in=reserve)),
        "outstanding_corrections": list(
            models.EconomicDeferral.objects.filter(epoch=epoch, resolution__isnull=True)
            .values("category")
            .annotate(count=Count("pk"))
        ),
        "reconciliation": reconcile_epoch(epoch) if epoch.state == "SETTLED" else None,
        "growth_events": list(
            models.GrowthEpochFunding.objects.filter(epoch=epoch)
            .order_by("event_id")
            .values("event_id", "amount")[:100]
        ),
        "categories_expected": list(CATEGORIES),
    }
