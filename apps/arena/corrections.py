"""One final, funded category correction round; original history never changes."""

from datetime import datetime
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.arena.economics import qualification_outcomes, require_permission
from apps.arena.models import (
    CategoryCorrectionReserve,
    CorrectionAssessment,
    DlxAllocation,
    DlxCorrection,
    DlxCorrectionAdjustment,
    DlxCorrectionAttempt,
    DlxCorrectionEntitlement,
    DlxCorrectionRound,
    EconomicDeferral,
    EconomicDeferralResolution,
    EconomicEpoch,
    EconomicRecognition,
    EconomicReviewDecision,
)
from apps.arena.policy import CATEGORIES, SOURCE_COMPONENTS, digest
from apps.arena.recognition import bound_policy, defer_source
from apps.arena.scoring import (
    creator_scores,
    emission_limit,
    player_scores,
    proportional_allocation,
)
from apps.seasons.barrier import acquire_close_barrier, acquire_economic_write_barrier


def _total(queryset: Any) -> int:
    return int(queryset.aggregate(total=Sum("amount"))["total"] or 0)


def corrected_category_scores(
    epoch: EconomicEpoch, *, dispositions: dict[int, tuple[str, str]] | None = None
) -> dict[int, dict[str, tuple[int, dict[str, Any]]]]:
    """Reproduce corrected eligibility/performance without writing retroactive normal receipts."""
    selected = bound_policy(epoch)
    assert selected is not None
    _, policy = selected
    existing = set(
        EconomicRecognition.objects.filter(epoch__season_id=epoch.season_id).values_list(
            "source_type", "source_id"
        )
    )
    extra: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for case in EconomicDeferral.objects.filter(epoch=epoch).prefetch_related("proofs"):
        identity = (case.provider, case.source_id)
        if case.provider not in SOURCE_COMPONENTS or identity in existing or identity in seen:
            continue
        payloads = [p.payload for p in case.proofs.all()]
        if not payloads:
            continue  # Unproven exposure remains a case, never a fabricated score.
        digests = {p["evidence_digest"] for p in payloads}
        if len(digests) != 1:
            raise ValidationError("CORRECTION_SOURCE_PROOFS_CONFLICT")
        proof = payloads[0]
        source = datetime.fromisoformat(proof["source_time"])
        finalized = datetime.fromisoformat(proof["finalized_at"])
        # The original in-Epoch event day is a correction attribution, not a new recognition.
        at = source if epoch.starts_at <= source < epoch.ends_at else finalized
        if not epoch.starts_at <= at < epoch.ends_at:
            raise ValidationError("CORRECTION_PROOF_OUTSIDE_ORIGINAL_EPOCH")
        category, component = SOURCE_COMPONENTS[case.provider]
        expected = digest(
            {
                "season": epoch.season_id,
                "player": case.player_id,
                "actor": proof["actor_id"],
                "source_time": source.isoformat(),
                "config": proof["config_version_id"],
                "inputs": proof["inputs"],
                "category": category,
            }
        )
        if expected != proof["evidence_digest"]:
            raise ValidationError("CORRECTION_PROOF_DIGEST_MISMATCH")
        extra.append(
            {
                "id": -case.pk,
                "player_id": case.player_id,
                "actor_id": proof["actor_id"],
                "source_type": case.provider,
                "source_id": case.source_id,
                "source_time": source,
                "finalized_at": finalized,
                "recognized_at": at,
                "component": component,
                "category": category,
                "inputs": proof["inputs"],
            }
        )
        seen.add(identity)
    outcomes, people, events = qualification_outcomes(epoch, policy, extra_records=extra)
    if dispositions is not None:
        dispositions.update(outcomes)
    qualified = {pid for pid, value in outcomes.items() if value[0] == "QUALIFIED"}
    anchored = {pid for pid, raw in people.items() if raw["anchor_days"]}
    creators = creator_scores(events, qualified, anchored, policy)
    return {pid: player_scores(people[pid], creators.get(pid, {}), policy) for pid in qualified}


@transaction.atomic
def assess_correction(
    case: EconomicDeferral,
    *,
    performance_units: int,
    actor: User,
    evidence_reference: str,
    creator_h_units: int = 0,
) -> CorrectionAssessment:
    """Review the complete corrected player/category score, not an XP amount or new reward rate."""
    require_permission(actor, "arena.approve_dlx_correction")
    acquire_economic_write_barrier(case.epoch.season_id)
    case = EconomicDeferral.objects.select_for_update().select_related("epoch").get(pk=case.pk)
    selected = bound_policy(case.epoch)
    if (
        selected is None
        or case.epoch.state != "SETTLED"
        or not case.player_id
        or case.category not in CATEGORIES
    ):
        raise ValidationError("Original settled category and beneficiary required")
    _, policy = selected
    if (
        type(performance_units) is not int
        or not 0 <= performance_units <= policy.number("scale")
        or type(creator_h_units) is not int
        or creator_h_units < 0
        or not evidence_reference.strip()
    ):
        raise ValidationError("Reviewed bounded score and evidence required")
    if CategoryCorrectionReserve.objects.filter(
        budget__epoch=case.epoch, budget__category=case.category, correction_round__isnull=False
    ).exists():
        raise ValidationError("CORRECTION_ADMISSION_CLOSED")
    if (
        case.provider == "PLAYER_REVIEW"
        and not EconomicReviewDecision.objects.filter(
            epoch=case.epoch, player_id=case.player_id, outcome="CLEARED"
        ).exists()
    ):
        raise ValidationError("INDIVIDUAL_REVIEW_NOT_CLEARED")
    if not case.proofs.exists() and not (
        case.provider == "PLAYER_REVIEW"
        and EconomicReviewDecision.objects.filter(
            epoch=case.epoch, player_id=case.player_id, outcome="CLEARED"
        ).exists()
    ):
        raise ValidationError("CORRECTION_SOURCE_PROOF_UNRESOLVED")
    all_reproduced = corrected_category_scores(case.epoch)
    reproduced = all_reproduced.get(case.player_id)
    if reproduced is None:
        raise ValidationError("CORRECTION_QUALIFICATION_UNPROVEN")
    expected_units, raw = reproduced[case.category]
    expected_h = int(raw.get("h_units", 0))
    if (performance_units, creator_h_units) != (expected_units, expected_h):
        raise ValidationError("CORRECTION_ASSESSMENT_NOT_REPRODUCIBLE")
    row, created = CorrectionAssessment.objects.get_or_create(
        deferral=case,
        input_digest=digest(all_reproduced),
        defaults={
            "performance_units": performance_units,
            "creator_h_units": creator_h_units,
            "actor": actor,
            "evidence_reference": evidence_reference,
        },
    )
    if not created and (
        row.performance_units,
        row.creator_h_units,
        row.actor_id,
        row.evidence_reference,
    ) != (performance_units, creator_h_units, actor.pk, evidence_reference):
        raise ValidationError("CORRECTION_ASSESSMENT_CONFLICT")
    return row


@transaction.atomic
def resolve_ineligible_correction(
    case: EconomicDeferral, *, actor: User, evidence_reference: str
) -> EconomicDeferralResolution:
    """Proven noneligibility has zero entitlement; missing or pending evidence does not."""
    require_permission(actor, "arena.approve_dlx_correction")
    acquire_close_barrier(case.epoch.season_id)
    case = EconomicDeferral.objects.select_for_update().select_related("epoch").get(pk=case.pk)
    if not evidence_reference.strip() or not case.player_id or case.category not in CATEGORIES:
        raise ValidationError("Original beneficiary/category and evidence required")
    if EconomicDeferral.objects.filter(
        epoch=case.epoch, category="", resolution__isnull=True
    ).exists():
        raise ValidationError("CORRECTION_SOURCE_UNIVERSE_NOT_FINALIZED")
    for exposure in EconomicDeferral.objects.filter(
        epoch=case.epoch, player_id=case.player_id, resolution__isnull=True
    ):
        if not exposure.proofs.exists() and not (
            exposure.provider == "PLAYER_REVIEW"
            and EconomicReviewDecision.objects.filter(
                epoch=case.epoch, player_id=case.player_id, outcome="DISQUALIFIED"
            ).exists()
        ):
            raise ValidationError("CORRECTION_PLAYER_EVIDENCE_UNRESOLVED")
    dispositions: dict[int, tuple[str, str]] = {}
    corrected_category_scores(case.epoch, dispositions=dispositions)
    outcome, reason = dispositions.get(case.player_id, ("UNKNOWN", "NO_PROOF"))
    if outcome not in {"NOT_YET_QUALIFIED", "DISQUALIFIED"}:
        raise ValidationError("CORRECTION_INELIGIBILITY_NOT_ESTABLISHED")
    row, created = EconomicDeferralResolution.objects.get_or_create(
        deferral=case,
        defaults={
            "outcome": "NO_ELIGIBLE_ENTITLEMENT",
            "actor": actor,
            "evidence_reference": f"{outcome}:{reason}:{evidence_reference}",
        },
    )
    if not created and row.outcome != "NO_ELIGIBLE_ENTITLEMENT":
        raise ValidationError("DEFERRAL_RESOLUTION_CONFLICT")
    return row


@transaction.atomic
def finalize_correction_round(
    epoch: EconomicEpoch, category: str, *, actor: User
) -> DlxCorrectionRound:
    """Seal all admitted corrected scores together after the 45-day admission window.

    Waiting for this final round does not delay any normal weekly settlement. Unknown cases
    remain unresolved; a final round cannot assert them resolved merely to close the Season.
    """
    require_permission(actor, "arena.approve_dlx_correction")
    acquire_close_barrier(epoch.season_id)
    epoch = EconomicEpoch.objects.select_for_update().select_related("season").get(pk=epoch.pk)
    selected = bound_policy(epoch)
    if selected is None or epoch.state != "SETTLED" or timezone.now() < epoch.season.ends_at:
        raise ValidationError("CORRECTION_ADMISSION_STILL_OPEN_OR_EPOCH_UNSETTLED")
    binding, policy = selected
    reserve = (
        CategoryCorrectionReserve.objects.select_for_update()
        .select_related("budget")
        .get(budget__epoch=epoch, budget__category=category)
    )
    existing = DlxCorrectionRound.objects.filter(reserve=reserve).first()
    if existing:
        return existing
    if EconomicDeferral.objects.filter(epoch=epoch, category="", resolution__isnull=True).exists():
        raise ValidationError("CORRECTION_SOURCE_UNIVERSE_NOT_FINALIZED")
    cases = list(
        EconomicDeferral.objects.filter(epoch=epoch, category=category, resolution__isnull=True)
        .prefetch_related("assessments")
        .order_by("pk")
    )
    if any(not hasattr(case, "assessment") for case in cases):
        raise ValidationError("ADMITTED_CORRECTION_ASSESSMENT_UNRESOLVED")
    reproduced = corrected_category_scores(epoch)
    minimum = policy.number("minimum_category_score")
    scores = {
        pid: categories[category][0]
        for pid, categories in reproduced.items()
        if categories[category][0] >= minimum
    }
    h_by_player = {
        pid: int(categories[category][1].get("h_units", 0))
        for pid, categories in reproduced.items()
    }
    primary_cases: dict[int, EconomicDeferral] = {}
    for case in cases:
        assert case.player_id is not None
        if case.player_id not in reproduced:
            raise ValidationError("CORRECTION_QUALIFICATION_UNPROVEN")
        actual, raw = reproduced[case.player_id][category]
        if (case.assessment.performance_units, case.assessment.creator_h_units) != (
            actual,
            int(raw.get("h_units", 0)),
        ):
            raise ValidationError("CORRECTION_ASSESSMENT_STALE")
        primary_cases.setdefault(case.player_id, case)
    budget = reserve.budget
    h = sum(h_by_player.get(pid, 0) for pid in scores)
    limit, _ = emission_limit(
        budget.base_epoch_budget, budget.carry_in, category, scores, h, policy
    )
    cap = budget.base_epoch_budget * policy.number("player_cap_bps") // policy.number("scale")
    corrected = proportional_allocation(scores, limit, cap)
    original = {
        r["player_id"]: int(r["total"])
        for r in DlxAllocation.objects.filter(epoch=epoch, category=category)
        .values("player_id")
        .annotate(total=Sum("amount"))
    }
    positive = {
        pid: value - original.get(pid, 0)
        for pid, value in corrected.items()
        if value > original.get(pid, 0)
    }
    calculation = {
        "method": "FINAL_CATEGORY_ROUND_V1",
        "policy_id": binding.policy_id,
        "policy_checksum": binding.policy.checksum,
        "admitted_case_ids": [c.pk for c in cases],
        "corrected_scores": {str(k): v for k, v in sorted(scores.items())},
        "corrected_h_units": h,
        "normal_emission_limit": limit,
        "cap": cap,
        "positive": {str(k): v for k, v in sorted(positive.items())},
    }
    row = DlxCorrectionRound.objects.create(
        reserve=reserve, actor=actor, input_digest=digest(calculation), calculation=calculation
    )
    for pid, amount in sorted(positive.items()):
        case = primary_cases.get(pid) or defer_source(
            epoch,
            "CORRECTION_ROUND",
            f"{row.pk}:{pid}",
            reason="RECONCILED_POSITIVE_DIFFERENCE",
            player_id=pid,
            category=category,
            evidence={"round_digest": row.input_digest},
        )
        DlxCorrectionEntitlement.objects.create(
            round=row,
            deferral=case,
            player_id=pid,
            category=category,
            amount=amount,
            actor=actor,
            evidence_reference=case.assessment.evidence_reference
            if hasattr(case, "assessment")
            else row.input_digest,
            idempotency_key=f"correction:{row.pk}:{pid}",
            calculation={
                "round_digest": row.input_digest,
                "corrected_total": corrected[pid],
                "original_total": original.get(pid, 0),
            },
        )
    for case in cases:
        if case.player_id not in positive:
            EconomicDeferralResolution.objects.create(
                deferral=case,
                outcome="NO_POSITIVE_DIFFERENCE",
                actor=actor,
                evidence_reference=row.input_digest,
            )
    return row


@transaction.atomic
def pay_correction_round(
    row: DlxCorrectionRound, *, actor: User, idempotency_key: str
) -> list[DlxCorrectionAttempt]:
    require_permission(actor, "arena.approve_dlx_correction")
    if not idempotency_key.strip() or len(idempotency_key) > 150:
        raise ValidationError("Correction payment identity required")
    row = DlxCorrectionRound.objects.select_related("reserve__budget__epoch").get(pk=row.pk)
    acquire_economic_write_barrier(row.reserve.budget.epoch.season_id)
    reserve = CategoryCorrectionReserve.objects.select_for_update().get(pk=row.reserve_id)
    entitlements = list(row.entitlements.select_related("player", "deferral").order_by("pk"))
    available = reserve.amount - _total(DlxCorrection.objects.filter(reserve=reserve))
    paid_ids = set(
        DlxCorrection.objects.filter(entitlement__round=row).values_list(
            "entitlement_id", flat=True
        )
    )
    required = sum(e.amount for e in entitlements if e.pk not in paid_ids)
    results = []
    for entitlement in entitlements:
        key = f"{idempotency_key}:{entitlement.pk}"
        existing = DlxCorrectionAttempt.objects.filter(idempotency_key=key).first()
        if existing:
            if existing.entitlement_id != entitlement.pk:
                raise ValidationError("CORRECTION_PAYMENT_IDEMPOTENCY_CONFLICT")
            results.append(existing)
            continue
        outcome = "INSUFFICIENT_RESERVE" if required > available else "ALREADY_PAID"
        if required <= available and entitlement.pk not in paid_ids:
            payment = DlxCorrection.objects.create(
                entitlement=entitlement, reserve=reserve, amount=entitlement.amount
            )
            if entitlement.player.moderation_state in {"BANNED", "SUSPENDED", "FLAGGED"}:
                record_correction_adjustment(
                    payment,
                    kind="HOLD",
                    amount=payment.amount,
                    actor=actor,
                    reason="Current moderation review",
                    evidence_reference=entitlement.evidence_reference,
                    idempotency_key=f"correction-review-hold:{payment.pk}",
                )
            EconomicDeferralResolution.objects.create(
                deferral=entitlement.deferral,
                outcome="FUNDED_CORRECTION_PAID",
                actor=actor,
                evidence_reference=entitlement.evidence_reference,
            )
            # Multiple source cases may contribute to one reviewed total score.
            for case in EconomicDeferral.objects.filter(
                epoch=reserve.budget.epoch,
                category=entitlement.category,
                player_id=entitlement.player_id,
                resolution__isnull=True,
                pk__in=row.calculation["admitted_case_ids"],
            ):
                EconomicDeferralResolution.objects.create(
                    deferral=case,
                    outcome="FUNDED_CORRECTION_PAID",
                    actor=actor,
                    evidence_reference=row.input_digest,
                )
            outcome = "PAID"
        results.append(
            DlxCorrectionAttempt.objects.create(
                entitlement=entitlement,
                idempotency_key=key,
                outcome=outcome,
                available_amount=available,
            )
        )
    return results


@transaction.atomic
def record_correction_adjustment(
    payment: DlxCorrection,
    *,
    kind: str,
    amount: int,
    actor: User,
    reason: str,
    evidence_reference: str,
    idempotency_key: str,
) -> DlxCorrectionAdjustment:
    require_permission(actor, "arena.approve_dlx_correction")
    acquire_economic_write_barrier(payment.entitlement.deferral.epoch.season_id)
    payment = DlxCorrection.objects.select_for_update().get(pk=payment.pk)
    existing = DlxCorrectionAdjustment.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if (
            existing.correction_id,
            existing.kind,
            existing.amount,
            existing.actor_id,
            existing.reason,
            existing.evidence_reference,
        ) != (payment.pk, kind, amount, actor.pk, reason, evidence_reference):
            raise ValidationError("CORRECTION_ADJUSTMENT_IDEMPOTENCY_CONFLICT")
        return existing
    if (
        type(amount) is not int
        or amount <= 0
        or not reason.strip()
        or not evidence_reference.strip()
    ):
        raise ValidationError("Positive adjustment and evidence required")
    totals = {
        r["kind"]: int(r["total"])
        for r in payment.adjustments.values("kind").annotate(total=Sum("amount"))
    }
    held = totals.get("HOLD", 0) - totals.get("RELEASE_HOLD", 0)
    forfeited = totals.get("FORFEIT", 0) - totals.get("REVERSAL", 0)
    if (
        (kind in {"HOLD", "FORFEIT"} and held + forfeited + amount > payment.amount)
        or (kind == "RELEASE_HOLD" and amount > held)
        or (kind == "REVERSAL" and amount > forfeited)
        or kind not in {"HOLD", "FORFEIT", "RELEASE_HOLD", "REVERSAL"}
    ):
        raise ValidationError("Correction adjustment exceeds credit")
    return DlxCorrectionAdjustment.objects.create(
        correction=payment,
        kind=kind,
        amount=amount,
        actor=actor,
        reason=reason,
        evidence_reference=evidence_reference,
        idempotency_key=idempotency_key,
    )
