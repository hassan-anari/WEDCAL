"""Season-to-date finalized qualification and atomic pre-Freeze Growth placement."""

from typing import Any

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.arena.models import (
    EconomicEpoch,
    EpochPerformanceSnapshot,
    GrowthEpochFunding,
    GrowthEvaluation,
    GrowthUnlockEvent,
    QualificationDecision,
)
from apps.arena.policy import digest
from apps.arena.recognition import bound_policy
from apps.game_config.services import resolve_game_config_version
from apps.seasons.barrier import acquire_close_barrier
from apps.seasons.models import Season

GROWTH_RULE = "SEASON_TO_DATE_PRE_FREEZE_EQUAL_V1"


def growth_cohort_queryset(epoch: EconomicEpoch) -> Any:
    """Canonical SQL counter shared by execution and read-only inspection."""
    selected = bound_policy(epoch)
    if selected is None:
        raise ValidationError("GROWTH_POLICY_UNBOUND")
    binding, _ = selected
    from apps.arena.readiness import policy_identity

    key, version = policy_identity(epoch)
    finalized = EpochPerformanceSnapshot.objects.filter(
        epoch__season_id=epoch.season_id,
        epoch__epoch_number__lte=epoch.epoch_number,
        epoch__state__in=["FROZEN", "SETTLING", "SETTLED"],
    ).values("qualification_decision_id")
    latest = (
        QualificationDecision.objects.filter(
            policy_key=key,
            policy_version=version,
            source="S1_FINALIZED_V1",
            effective_at__lte=epoch.ends_at,
            evidence__epoch_id__in=list(
                EconomicEpoch.objects.filter(
                    season_id=epoch.season_id, epoch_number__lte=epoch.epoch_number
                ).values_list("pk", flat=True)
            ),
        )
        .order_by("player_id", "-effective_at", "-pk")
        .distinct("player_id")
    )
    adverse = QualificationDecision.objects.filter(
        pk__in=latest.values("pk"), decision__in=["PENDING", "DISQUALIFIED"]
    ).values("player_id")
    return (
        QualificationDecision.objects.filter(
            Q(pk__in=finalized) | Q(evidence__epoch_id=epoch.pk, source="S1_FINALIZED_V1"),
            policy_key=key,
            policy_version=version,
            decision="QUALIFIED",
            evidence__policy_checksum=binding.policy.checksum,
            effective_at__lte=epoch.ends_at,
        )
        .exclude(player_id__in=adverse)
        .order_by("player_id", "-effective_at", "-pk")
        .distinct("player_id")
    )


def season_qualification_evidence(
    epoch: EconomicEpoch,
) -> tuple[list[tuple[int, int]], dict[str, Any]]:
    """Prior inactive participation persists; current adverse dispositions exclude future counts."""
    selected = bound_policy(epoch)
    assert selected is not None
    binding, _ = selected
    cohort = list(growth_cohort_queryset(epoch).values_list("player_id", "pk"))
    return cohort, {
        "recognition_epoch_id": epoch.pk,
        "recognition_epoch_number": epoch.epoch_number,
        "qualifying_cutoff": epoch.ends_at.isoformat(),
        "cohort_digest": digest(cohort),
        "counted_population": "DISTINCT_SEASON_TO_DATE_FINALIZED_QUALIFIED_MINUS_CURRENT_ADVERSE",
        "policy_id": binding.policy_id,
        "policy_checksum": binding.policy.checksum,
    }


@transaction.atomic
def unlock_growth(season: Season, *, epoch: EconomicEpoch | None = None) -> list[GrowthUnlockEvent]:
    acquire_close_barrier(season.pk)
    season = Season.objects.select_for_update().select_related("baseline_config").get(pk=season.pk)
    setup = resolve_game_config_version(season.baseline_config_id).dlx_distribution
    if setup is None or not setup.growth_milestones:
        return []
    if season.status != Season.Status.ACTIVE:
        raise ValidationError("GROWTH_REQUIRES_ACTIVE_SEASON")
    if epoch is None:
        epoch = (
            season.economic_epochs.filter(
                ends_at__lte=timezone.now(),
                source_manifests__isnull=False,
            )
            .order_by("-epoch_number")
            .first()
        )
    if epoch is None:
        return []
    epoch = EconomicEpoch.objects.select_for_update().get(pk=epoch.pk, season=season)
    selected = bound_policy(epoch)
    if selected is None or selected[1].values.get("growth", {}).get("rule") != GROWTH_RULE:
        raise ValidationError("GROWTH_POLICY_NOT_APPROVED")
    binding, policy = selected
    if epoch.ends_at > timezone.now():
        raise ValidationError("GROWTH_QUALIFICATION_CUTOFF_NOT_REACHED")
    if epoch.state in {"OPEN", "DRAINING"}:
        from apps.arena.readiness import readiness_blockers

        reasons, evaluation = readiness_blockers(epoch)
        if reasons or evaluation is None:
            raise ValidationError("GROWTH_AUTHORITATIVE_COHORT_UNPROVEN")
        evaluation_id = evaluation.pk
    else:
        evaluation_id = epoch.freeze_watermark.get("readiness_evaluation_id")
        if not evaluation_id:
            raise ValidationError("GROWTH_FINALIZED_COHORT_UNPROVEN")
    cohort, proof = season_qualification_evidence(epoch)
    count = len(cohort)
    # Temporal provenance, not merely an OPEN label: current cutoff's own DRAINING Epoch or
    # later periods beginning at/after that cutoff. Earlier stale OPEN Epochs are excluded.
    receiving = list(
        EconomicEpoch.objects.select_for_update(of=("self",))
        .filter(
            Q(pk=epoch.pk, state="DRAINING")
            | Q(
                epoch_number__gt=epoch.epoch_number,
                starts_at__gte=epoch.ends_at,
                state__in=["OPEN", "DRAINING"],
            ),
            season=season,
            category_budgets__isnull=True,
            performance_snapshots__isnull=True,
        )
        .order_by("epoch_number")
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('dexo.phase4_growth', 'CONFIGURABLE_WEEKLY_V1', true)")
    unlocked = int(season.growth_unlocks.aggregate(v=Sum("unlocked_amount"))["v"] or 0)
    result = []
    for milestone in sorted(
        setup.growth_milestones, key=lambda m: (m.qualified_player_count, m.key)
    ):
        if (
            count < milestone.qualified_player_count
            or season.growth_unlocks.filter(milestone_key=milestone.key).exists()
        ):
            continue
        amount = milestone.unlock_dlx_amount if receiving else 0
        if setup.base_pool_dlx_amount + unlocked + amount > setup.max_pool_dlx_amount:
            raise ValidationError("GROWTH_AUTHORIZATION_EXCEEDED")
        portions = {}
        if receiving:
            quotient, remainder = divmod(amount, len(receiving))
            portions = {row.pk: quotient + int(i < remainder) for i, row in enumerate(receiving)}
        event = GrowthUnlockEvent.objects.create(
            season=season,
            milestone_key=milestone.key,
            qualified_player_count=count,
            unlocked_amount=amount,
            policy_key=setup.qualified_player_predicate["policy_key"],
            policy_version=setup.qualified_player_predicate["policy_version"],
            config_version_id=season.baseline_config_id,
            evidence={
                **proof,
                "rule": GROWTH_RULE,
                "rule_approval": policy.values["growth"]["approval_reference"],
                "config_checksum": season.baseline_config.payload_checksum,
                "evaluation_id": evaluation_id,
                "evaluated_at": timezone.now().isoformat(),
                "threshold": milestone.qualified_player_count,
                "requested_amount": milestone.unlock_dlx_amount,
                "outcome": "FUNDED" if receiving else "NO_ELIGIBLE_MUTABLE_EPOCH",
                "placement": {str(key): value for key, value in portions.items()},
            },
        )
        for row in receiving:
            portion = portions[row.pk]
            if portion:
                GrowthEpochFunding.objects.create(event=event, epoch=row, amount=portion)
                row.growth_budget += portion
                row.save(update_fields=("growth_budget",))
        unlocked += amount
        result.append(event)
    GrowthEvaluation.objects.create(
        season=season,
        epoch=epoch,
        policy=binding.policy,
        cutoff_at=epoch.ends_at,
        qualified_player_count=count,
        cohort_digest=proof["cohort_digest"],
        config_checksum=season.baseline_config.payload_checksum,
        outcome="FUNDED"
        if any(event.unlocked_amount for event in result)
        else "NO_ELIGIBLE_MUTABLE_EPOCH"
        if result
        else "REPLAY"
        if any(count >= item.qualified_player_count for item in setup.growth_milestones)
        else "THRESHOLD_NOT_REACHED",
    )
    return result
