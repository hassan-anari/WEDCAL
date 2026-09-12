from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.clans.governance import _close_membership
from apps.clans.models import ClanMembership, ClanSeason
from apps.game_config.services import resolve_effective_game_config, resolve_game_config_version
from apps.moderation.models import ModerationAction, ModerationCase, ModerationEvidence
from apps.network.models import NetworkActivity
from apps.players.models import Player


class ModerationIdempotencyConflict(ValidationError):
    pass


@transaction.atomic
def create_report(
    *,
    reporter: Player,
    target: Player,
    category: str,
    reason: str,
    network_activity_id: int | None = None,
) -> ModerationCase:
    category, reason = category.strip(), reason.strip()
    if reporter.pk == target.pk:
        raise ValidationError("Self-report is not supported")
    if not category or len(category) > 32 or not reason or len(reason) > 1000:
        raise ValidationError("Invalid report category or reason")
    activity = None
    clan_season = None
    if network_activity_id is not None:
        activity = (
            NetworkActivity.objects.select_related("group").filter(pk=network_activity_id).first()
        )
        if activity is None or activity.sender_id != target.pk:
            raise PermissionDenied("Evidence does not belong to the reported player")
        clan_season = ClanSeason.objects.filter(
            clan__telegram_group=activity.group,
            season=activity.season,
            state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
        ).first()
    case = ModerationCase.objects.create(
        reporter=reporter,
        target=target,
        category=category,
        reason=reason,
        config_version_id=resolve_effective_game_config(timezone.now()).id,
        telegram_group=activity.group if activity else None,
        clan_season=clan_season,
    )
    if activity:
        ModerationEvidence.objects.create(
            case=case,
            kind=ModerationEvidence.Kind.NETWORK_ACTIVITY,
            object_id=activity.pk,
            verified_server_observation=True,
        )
    return case


def _same_action(
    action: ModerationAction,
    *,
    case_id: int,
    target_id: int,
    action_type: str,
    reason: str,
    actor_id: int | None,
    system_actor: str,
) -> bool:
    return (
        action.case_id == case_id
        and action.target_id == target_id
        and action.action_type == action_type
        and action.reason == reason
        and action.actor_id == actor_id
        and action.system_actor == system_actor
    )


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
    if action_type not in ModerationAction.Type.values or not reason.strip() or not idempotency_key:
        raise ValidationError("Invalid moderation action")
    with transaction.atomic():
        # Serialize the globally unique key before any case/player row locks.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [idempotency_key]
            )
        existing = ModerationAction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            if not _same_action(
                existing,
                case_id=case_id,
                target_id=existing.target_id,
                action_type=action_type,
                reason=reason,
                actor_id=actor.pk if actor else None,
                system_actor=system_actor,
            ):
                raise ModerationIdempotencyConflict("Idempotency key belongs to another action")
            return existing
        case = ModerationCase.objects.select_for_update().get(pk=case_id)
        if case.status == ModerationCase.Status.CLOSED:
            raise ValidationError("Moderation case is closed")
        target = Player.objects.select_for_update().get(pk=case.target_id)
        previous: dict[str, object] = {
            "moderation_state": target.moderation_state,
            "case_status": case.status,
        }
        transitions: dict[str, str] = {
            str(ModerationAction.Type.FLAG): str(Player.ModerationState.FLAGGED),
            str(ModerationAction.Type.SUSPEND): str(Player.ModerationState.SUSPENDED),
            str(ModerationAction.Type.BAN): str(Player.ModerationState.BANNED),
        }
        desired = transitions.get(action_type)
        severity: dict[str, int] = {
            Player.ModerationState.ACTIVE: 0,
            Player.ModerationState.FLAGGED: 1,
            Player.ModerationState.SUSPENDED: 2,
            Player.ModerationState.BANNED: 3,
        }
        if desired and severity[desired] > severity[target.moderation_state]:
            target.moderation_state = desired
            target.save(update_fields=("moderation_state", "updated_at"))
        if action_type == ModerationAction.Type.REMOVE_FROM_CLAN:
            if case.clan_season_id is None:
                raise ValidationError("Clan context is required for Clan removal")
            membership = (
                ClanMembership.objects.select_for_update()
                .filter(player=target, clan_season_id=case.clan_season_id, ended_at__isnull=True)
                .first()
            )
            if membership is None:
                raise ValidationError("Target has no current membership in the case Clan")
            previous["membership"] = {
                "membership_id": membership.pk,
                "ended_at": None,
                "end_reason": membership.end_reason,
            }
            _close_membership(
                membership,
                ClanMembership.EndReason.MODERATION_REMOVAL,
                timezone.now(),
                source_type="MODERATION",
                source_id=str(case.pk),
            )
        if action_type == ModerationAction.Type.DISMISS:
            close_case(case)
        resulting: dict[str, object] = {
            "moderation_state": target.moderation_state,
            "case_status": case.status,
        }
        if action_type == ModerationAction.Type.REMOVE_FROM_CLAN:
            assert membership is not None
            membership.refresh_from_db()
            resulting["membership"] = {
                "membership_id": membership.pk,
                "ended_at": membership.ended_at.isoformat() if membership.ended_at else None,
                "end_reason": membership.end_reason,
            }
        return ModerationAction.objects.create(
            case=case,
            actor=actor,
            system_actor=system_actor,
            target=target,
            action_type=action_type,
            reason=reason,
            previous_state=previous,
            resulting_state=resulting,
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
