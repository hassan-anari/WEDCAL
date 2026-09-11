from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.clans.governance import _close_membership
from apps.clans.models import ClanMembership
from apps.game_config.services import resolve_effective_game_config, resolve_game_config_version
from apps.moderation.models import ModerationAction, ModerationCase, ModerationEvidence
from apps.network.models import NetworkActivity
from apps.players.models import Player


@transaction.atomic
def create_report(
    *,
    reporter: Player,
    target: Player,
    category: str,
    reason: str,
    network_activity_id: int | None = None,
) -> ModerationCase:
    if reporter.pk == target.pk:
        raise ValidationError("Self-report is not supported")
    if not category.strip() or not reason.strip():
        raise ValidationError("Category and reason are required")
    case = ModerationCase.objects.create(
        reporter=reporter,
        target=target,
        category=category[:32],
        reason=reason,
        config_version_id=resolve_effective_game_config(timezone.now()).id,
    )
    if network_activity_id is not None:
        activity = NetworkActivity.objects.filter(pk=network_activity_id).first()
        if activity is None:
            raise ValidationError("Evidence reference is unavailable")
        ModerationEvidence.objects.create(
            case=case,
            kind=ModerationEvidence.Kind.NETWORK_ACTIVITY,
            object_id=activity.pk,
            verified_server_observation=True,
        )
    return case


def apply_action(
    *,
    case_id: int,
    action_type: str,
    reason: str,
    idempotency_key: str,
    actor: Player | None = None,
    system_actor: str = "",
) -> ModerationAction:
    if actor is None and not system_actor:
        raise PermissionDenied("An actor is required")
    if action_type not in ModerationAction.Type.values:
        raise ValidationError("Unsupported moderation action")
    with transaction.atomic():
        existing = ModerationAction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing
        case = ModerationCase.objects.select_for_update().select_related("target").get(pk=case_id)
        target = Player.objects.select_for_update().get(pk=case.target_id)
        previous = {"moderation_state": target.moderation_state}
        if action_type == ModerationAction.Type.FLAG:
            target.moderation_state = Player.ModerationState.FLAGGED
        elif action_type == ModerationAction.Type.SUSPEND:
            target.moderation_state = Player.ModerationState.SUSPENDED
        elif action_type == ModerationAction.Type.BAN:
            target.moderation_state = Player.ModerationState.BANNED
        if target.moderation_state != previous["moderation_state"]:
            target.save(update_fields=("moderation_state", "updated_at"))
        if action_type == ModerationAction.Type.REMOVE_FROM_CLAN:
            membership = (
                ClanMembership.objects.select_for_update()
                .filter(player=target, ended_at__isnull=True)
                .first()
            )
            if membership:
                _close_membership(
                    membership,
                    ClanMembership.EndReason.MODERATION_REMOVAL,
                    timezone.now(),
                    source_type="MODERATION",
                    source_id=str(case.pk),
                )
        if action_type == ModerationAction.Type.DISMISS:
            close_case(case)
        return ModerationAction.objects.create(
            case=case,
            actor=actor,
            system_actor=system_actor,
            target=target,
            action_type=action_type,
            reason=reason,
            previous_state=previous,
            resulting_state={"moderation_state": target.moderation_state},
            idempotency_key=idempotency_key,
        )


def close_case(case: ModerationCase, *, now: datetime | None = None) -> ModerationCase:
    now = now or timezone.now()
    if case.status == ModerationCase.Status.CLOSED:
        return case
    days = resolve_game_config_version(
        case.config_version_id
    ).retention.moderation_evidence_after_close_days
    case.status, case.closed_at, case.evidence_retain_until = (
        ModerationCase.Status.CLOSED,
        now,
        now + timedelta(days=days),
    )
    case.save(update_fields=("status", "closed_at", "evidence_retain_until"))
    return case


def protected_network_activity_ids(now: datetime | None = None):  # type: ignore[no-untyped-def]
    now = now or timezone.now()
    return (
        ModerationEvidence.objects.filter(kind=ModerationEvidence.Kind.NETWORK_ACTIVITY)
        .filter(Q(case__status=ModerationCase.Status.OPEN) | Q(case__evidence_retain_until__gt=now))
        .values_list("object_id", flat=True)
    )
