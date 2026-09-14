from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.arena.models import DlxAdjustment, EpochAnomaly, EpochAnomalyResolution


@transaction.atomic
def resolve_epoch_anomaly(
    anomaly: EpochAnomaly,
    *,
    outcome: str,
    reason: str,
    idempotency_key: str,
    actor: User | None = None,
    service_actor: str = "",
    evidence_reference: str = "",
    adjustment: DlxAdjustment | None = None,
) -> tuple[EpochAnomalyResolution, bool]:
    if actor is not None and not actor.has_perm("arena.resolve_epoch_anomaly"):
        raise PermissionDenied("Epoch anomaly resolution permission required")
    if actor is None and not service_actor.strip():
        raise ValidationError("An anomaly resolution actor is required")
    if not reason.strip() or not idempotency_key.strip():
        raise ValidationError("Resolution reason and idempotency key are required")
    anomaly = EpochAnomaly.objects.select_for_update().get(pk=anomaly.pk)
    existing = EpochAnomalyResolution.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if (
            existing.anomaly_id != anomaly.pk
            or existing.outcome != outcome
            or existing.reason != reason
            or existing.evidence_reference != evidence_reference
            or existing.adjustment_id != (adjustment.pk if adjustment else None)
            or existing.actor_id != (actor.pk if actor else None)
            or existing.service_actor != (service_actor if actor is None else "")
        ):
            raise ValidationError("Anomaly resolution idempotency conflict")
        return existing, False
    if hasattr(anomaly, "resolution"):
        raise ValidationError("Anomaly is already resolved")
    if outcome not in EpochAnomalyResolution.Outcome.values:
        raise ValidationError("Unsupported anomaly resolution outcome")
    if outcome == EpochAnomalyResolution.Outcome.ADJUSTMENT_RECORDED and adjustment is None:
        raise ValidationError("Adjustment-recorded outcome requires an existing adjustment")
    if adjustment is not None and adjustment.allocation.epoch_id != anomaly.epoch_id:
        raise ValidationError("Linked adjustment must belong to the anomaly epoch")
    return EpochAnomalyResolution.objects.create(
        anomaly=anomaly,
        outcome=outcome,
        actor=actor,
        service_actor=service_actor if actor is None else "",
        reason=reason,
        evidence_reference=evidence_reference,
        adjustment=adjustment,
        idempotency_key=idempotency_key,
    ), True
