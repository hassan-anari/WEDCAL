from django.db import connection, transaction
from django.db.models import Sum

from apps.arena.models import DlxAdjustment, DlxAllocation
from apps.players.models import Player
from apps.seasons.models import Season


@transaction.atomic
def record_adjustment(
    *,
    allocation: DlxAllocation,
    kind: str,
    amount: int,
    reason_code: str,
    idempotency_key: str,
    actor: Player | None = None,
    system_actor: str = "",
    evidence_reference: str = "",
    metadata: dict[str, object] | None = None,
) -> tuple[DlxAdjustment, bool]:
    from apps.seasons.barrier import acquire_economic_write_barrier

    acquire_economic_write_barrier(allocation.season_id)
    if kind not in DlxAdjustment.Kind.values or amount <= 0:
        raise ValueError("Invalid DLX adjustment")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"dlx-adjustment:{idempotency_key}"],
        )
    safe_metadata = metadata or {}
    existing = DlxAdjustment.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if (
            existing.allocation_id,
            existing.kind,
            existing.amount,
            existing.reason_code,
            existing.evidence_reference,
            existing.actor_player_id,
            existing.system_actor,
            existing.metadata,
        ) != (
            allocation.pk,
            kind,
            amount,
            reason_code,
            evidence_reference,
            actor.pk if actor else None,
            system_actor,
            safe_metadata,
        ):
            raise ValueError("DLX adjustment idempotency conflict")
        return existing, False
    allocation = DlxAllocation.objects.select_for_update().get(pk=allocation.pk)
    totals = {
        row["kind"]: row["v"]
        for row in allocation.adjustments.values("kind").annotate(v=Sum("amount"))
    }
    held = totals.get(DlxAdjustment.Kind.HOLD, 0) - totals.get(DlxAdjustment.Kind.RELEASE_HOLD, 0)
    forfeited = totals.get(DlxAdjustment.Kind.FORFEIT, 0) - totals.get(
        DlxAdjustment.Kind.REVERSAL, 0
    )
    if (
        kind in {DlxAdjustment.Kind.HOLD, DlxAdjustment.Kind.FORFEIT}
        and held + forfeited + amount > allocation.amount
    ):
        raise ValueError("Adjustment exceeds allocation")
    if kind == DlxAdjustment.Kind.RELEASE_HOLD and amount > held:
        raise ValueError("Hold release exceeds held amount")
    if kind == DlxAdjustment.Kind.REVERSAL and amount > forfeited:
        raise ValueError("Reversal exceeds forfeited amount")
    return DlxAdjustment.objects.create(
        allocation=allocation,
        kind=kind,
        amount=amount,
        reason_code=reason_code,
        evidence_reference=evidence_reference,
        actor_player=actor,
        system_actor=system_actor,
        config_version=allocation.config_version,
        idempotency_key=idempotency_key,
        metadata=safe_metadata,
    ), True


def adjustment_totals(player: Player, season: Season) -> dict[str, int]:
    rows = (
        DlxAdjustment.objects.filter(allocation__player=player, allocation__season=season)
        .values("kind")
        .annotate(v=Sum("amount"))
    )
    values = {row["kind"]: row["v"] for row in rows}
    held = values.get("HOLD", 0) - values.get("RELEASE_HOLD", 0)
    forfeited = values.get("FORFEIT", 0) - values.get("REVERSAL", 0)
    return {"held": held, "forfeited": forfeited}


def season_adjustment_totals(season: Season) -> dict[str, int]:
    rows = (
        DlxAdjustment.objects.filter(allocation__season=season)
        .values("kind")
        .annotate(v=Sum("amount"))
    )
    values = {row["kind"]: int(row["v"] or 0) for row in rows}
    holds = values.get(DlxAdjustment.Kind.HOLD, 0)
    released = values.get(DlxAdjustment.Kind.RELEASE_HOLD, 0)
    forfeits = values.get(DlxAdjustment.Kind.FORFEIT, 0)
    reversed_amount = values.get(DlxAdjustment.Kind.REVERSAL, 0)
    return {
        "held": holds - released,
        "hold_recorded": holds,
        "released": released,
        "forfeited": forfeits - reversed_amount,
        "forfeit_recorded": forfeits,
        "reversed": reversed_amount,
    }
