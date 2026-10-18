from datetime import datetime

from django.db import IntegrityError, transaction

from apps.arena.models import EconomicEpoch, QualificationDecision
from apps.moderation.services import moderation_state_at
from apps.players.models import Player
from apps.seasons.barrier import acquire_economic_write_barrier

BLOCKED_STATES = {Player.ModerationState.SUSPENDED, Player.ModerationState.BANNED}


def _same_decision(
    row: QualificationDecision,
    *,
    player: Player,
    policy_version: int,
    decision: str,
    effective_at: datetime,
    reason_code: str,
    evidence: dict[str, object],
) -> bool:
    return (
        row.player_id == player.pk
        and row.policy_version == policy_version
        and row.decision == decision
        and row.effective_at == effective_at
        and row.reason_code == reason_code
        and row.evidence == evidence
    )


def record_qualification_decision(
    *,
    player: Player,
    policy_key: str,
    policy_version: int,
    decision: str,
    effective_at: datetime,
    reason_code: str,
    source: str,
    source_id: str,
    evidence: dict[str, object] | None = None,
) -> tuple[QualificationDecision, bool]:
    """Append an authoritative decision; duplicate source identities are strictly idempotent."""
    if decision not in QualificationDecision.Decision.values:
        raise ValueError("Unknown qualification decision")
    safe_evidence = evidence or {}
    existing = QualificationDecision.objects.filter(
        policy_key=policy_key, source=source, source_id=source_id
    ).first()
    if existing is not None:
        if not _same_decision(
            existing,
            player=player,
            policy_version=policy_version,
            decision=decision,
            effective_at=effective_at,
            reason_code=reason_code,
            evidence=safe_evidence,
        ):
            raise ValueError("Qualification decision idempotency conflict")
        return existing, False
    try:
        with transaction.atomic():
            affected = EconomicEpoch.objects.filter(
                ends_at__gte=effective_at,
                config_version__payload__dlx_distribution__pool__growth_unlock__qualified_player_predicate__policy_key=policy_key,
                config_version__payload__dlx_distribution__pool__growth_unlock__qualified_player_predicate__policy_version=policy_version,
            )
            for season_id in (
                affected.order_by("season_id").values_list("season_id", flat=True).distinct()
            ):
                acquire_economic_write_barrier(season_id)
            if affected.filter(state__in=["FROZEN", "SETTLING", "SETTLED"]).exists():
                raise ValueError("QUALIFICATION_CUTOFF_FINALIZED")
            return QualificationDecision.objects.create(
                player=player,
                policy_key=policy_key,
                policy_version=policy_version,
                decision=decision,
                effective_at=effective_at,
                reason_code=reason_code,
                source=source,
                source_id=source_id,
                evidence=safe_evidence,
            ), True
    except IntegrityError:
        row = QualificationDecision.objects.get(
            policy_key=policy_key, source=source, source_id=source_id
        )
        if not _same_decision(
            row,
            player=player,
            policy_version=policy_version,
            decision=decision,
            effective_at=effective_at,
            reason_code=reason_code,
            evidence=safe_evidence,
        ):
            raise ValueError("Qualification decision idempotency conflict") from None
        return row, False


def current_qualification_decision(
    player: Player, *, policy_key: str, policy_version: int, at: datetime
) -> QualificationDecision | None:
    return (
        QualificationDecision.objects.filter(
            player=player,
            policy_key=policy_key,
            policy_version=policy_version,
            effective_at__lte=at,
        )
        .order_by("-effective_at", "-id")
        .first()
    )


def is_player_qualified(
    player: Player, *, policy_key: str, policy_version: int, at: datetime
) -> bool:
    decision = current_qualification_decision(
        player, policy_key=policy_key, policy_version=policy_version, at=at
    )
    return bool(
        decision
        and decision.decision == QualificationDecision.Decision.QUALIFIED
        and moderation_state_at(player, at) not in BLOCKED_STATES
    )


def qualified_player_ids_at(
    *, policy_key: str, policy_version: int, at: datetime
) -> tuple[int, ...]:
    latest = (
        QualificationDecision.objects.filter(
            policy_key=policy_key, policy_version=policy_version, effective_at__lte=at
        )
        .order_by("player_id", "-effective_at", "-id")
        .distinct("player_id")
    )
    return tuple(
        row.player_id
        for row in latest.select_related("player")
        if row.decision == QualificationDecision.Decision.QUALIFIED
        and moderation_state_at(row.player, at) not in BLOCKED_STATES
    )
