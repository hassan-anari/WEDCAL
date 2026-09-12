from datetime import datetime

from django.db import IntegrityError, transaction

from apps.arena.models import QualificationDecision
from apps.players.models import Player


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
    """Append an authoritative decision; duplicate source identities are idempotent."""
    if decision not in QualificationDecision.Decision.values:
        raise ValueError("Unknown qualification decision")
    try:
        with transaction.atomic():
            return QualificationDecision.objects.create(
                player=player,
                policy_key=policy_key,
                policy_version=policy_version,
                decision=decision,
                effective_at=effective_at,
                reason_code=reason_code,
                source=source,
                source_id=source_id,
                evidence=evidence or {},
            ), True
    except IntegrityError:
        row = QualificationDecision.objects.get(
            policy_key=policy_key, source=source, source_id=source_id
        )
        expected = (player.pk, policy_version, decision, effective_at, reason_code)
        actual = (
            row.player_id,
            row.policy_version,
            row.decision,
            row.effective_at,
            row.reason_code,
        )
        if actual != expected:
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
    if player.moderation_state in {Player.ModerationState.SUSPENDED, Player.ModerationState.BANNED}:
        return False
    decision = current_qualification_decision(
        player, policy_key=policy_key, policy_version=policy_version, at=at
    )
    return bool(decision and decision.decision == QualificationDecision.Decision.QUALIFIED)
