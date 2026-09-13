# mypy: disable-error-code="misc"
import uuid
from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.clans.governance import _close_membership
from apps.clans.models import ClanMembership, ClanSeason
from apps.game_config.services import resolve_effective_game_config, resolve_game_config_version
from apps.moderation.models import (
    ModerationAction,
    ModerationCase,
    ModerationEvidence,
    ModerationEvidenceAccess,
)
from apps.network.models import NetworkActivity
from apps.players.models import Player


class ModerationIdempotencyConflict(ValidationError):
    pass


def moderation_state_at(player: Player, at: datetime) -> str:
    """Resolve audited moderation state at source time without using today's projection."""
    action = (
        ModerationAction.objects.filter(target=player, created_at__lte=at)
        .order_by("-created_at", "-id")
        .values_list("resulting_state", flat=True)
        .first()
    )
    if action is None:
        # Phase-7 rows may predate auditable actions; fail closed for a legacy blocked projection.
        has_any_audit = ModerationAction.objects.filter(target=player).exists()
        return Player.ModerationState.ACTIVE if has_any_audit else player.moderation_state
    return str(action.get("moderation_state", Player.ModerationState.ACTIVE))


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
    staff_actor_id: int | None,
    suspension_expires_at: datetime | None,
    correlation_id: uuid.UUID | None,
) -> bool:
    return (
        action.case_id == case_id
        and action.target_id == target_id
        and action.action_type == action_type
        and action.reason == reason
        and action.actor_id == actor_id
        and action.system_actor == system_actor
        and action.staff_actor_id == staff_actor_id
        and action.suspension_expires_at == suspension_expires_at
        and (correlation_id is None or action.correlation_id == correlation_id)
    )


def apply_action(
    *,
    case_id: int,
    action_type: str,
    reason: str,
    idempotency_key: str,
    actor: Player | None = None,
    system_actor: str = "",
    staff_actor: User | None = None,
    suspension_expires_at: datetime | None = None,
    correlation_id: uuid.UUID | None = None,
) -> ModerationAction:
    if actor is None and staff_actor is None and not system_actor:
        raise PermissionDenied("An actor is required")
    if staff_actor is not None and not staff_actor.has_perm("moderation.apply_moderation_actions"):
        raise PermissionDenied("Moderation action permission required")
    if action_type not in ModerationAction.Type.values or not reason.strip() or not idempotency_key:
        raise ValidationError("Invalid moderation action")
    if action_type == ModerationAction.Type.SUSPEND and (
        suspension_expires_at is None or suspension_expires_at <= timezone.now()
    ):
        raise ValidationError("A future suspension expiry is required")
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
                staff_actor_id=getattr(staff_actor, "pk", None),
                suspension_expires_at=suspension_expires_at,
                correlation_id=correlation_id,
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
        action = ModerationAction.objects.create(
            case=case,
            actor=actor,
            system_actor=system_actor,
            staff_actor=staff_actor,
            target=target,
            action_type=action_type,
            reason=reason,
            previous_state=previous,
            resulting_state=resulting,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id or uuid.uuid4(),
            suspension_expires_at=suspension_expires_at,
        )
        if desired == Player.ModerationState.SUSPENDED and target.moderation_state == desired:
            target.suspension_expires_at = suspension_expires_at
            target.suspension_action = action
            target.save(update_fields=("suspension_expires_at", "suspension_action", "updated_at"))
        elif desired == Player.ModerationState.BANNED:
            target.suspension_expires_at = None
            target.suspension_action = None
            target.save(update_fields=("suspension_expires_at", "suspension_action", "updated_at"))
        if action_type in {
            ModerationAction.Type.WARN,
            ModerationAction.Type.SUSPEND,
            ModerationAction.Type.BAN,
        }:
            from apps.notifications.services import emit_notification_safely

            transaction.on_commit(
                lambda recipient=target, created_action=action: emit_notification_safely(
                    recipient=recipient,
                    event_key=f"MODERATION_{created_action.action_type}",
                    source_domain="MODERATION",
                    source_type="MODERATION_ACTION",
                    source_id=str(created_action.pk),
                    idempotency_key=f"moderation-action:{created_action.pk}",
                    telegram=True,
                )
            )
        return action


def expire_suspensions(*, now: datetime | None = None, limit: int = 100) -> int:
    now = now or timezone.now()
    expired = 0
    candidates = list(
        Player.objects.filter(
            moderation_state=Player.ModerationState.SUSPENDED,
            suspension_expires_at__lte=now,
            suspension_action__isnull=False,
        )
        .order_by("suspension_expires_at")
        .values_list("id", "suspension_action_id", "suspension_action__case_id")[:limit]
    )
    for player_id, source_action_id, case_id in candidates:
        with transaction.atomic():
            # Canonical moderation transition order: case first, target Player second.
            case = ModerationCase.objects.select_for_update().get(pk=case_id)
            player = Player.objects.select_for_update().get(pk=player_id)
            if (
                player.moderation_state != Player.ModerationState.SUSPENDED
                or player.suspension_expires_at is None
                or player.suspension_expires_at > now
                or player.suspension_action_id != source_action_id
            ):
                continue
            source = ModerationAction.objects.filter(
                pk=source_action_id,
                case=case,
                target=player,
                action_type=ModerationAction.Type.SUSPEND,
                suspension_expires_at=player.suspension_expires_at,
            ).first()
            if source is None:
                continue
            player.moderation_state = Player.ModerationState.ACTIVE
            player.suspension_expires_at = None
            player.suspension_action = None
            player.save(
                update_fields=(
                    "moderation_state",
                    "suspension_expires_at",
                    "suspension_action",
                    "updated_at",
                )
            )
            ModerationAction.objects.create(
                case=case,
                system_actor="suspension-expiry",
                target=player,
                action_type=ModerationAction.Type.DISMISS,
                reason="Temporary suspension expired",
                previous_state={
                    "moderation_state": "SUSPENDED",
                    "suspension_action_id": source.pk,
                },
                resulting_state={"moderation_state": "ACTIVE"},
                idempotency_key=f"suspension-expiry:{source.pk}",
            )
            expired += 1
    return expired


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


def view_sensitive_evidence(
    *, evidence_id: int, actor: User, reason: str, now: datetime | None = None
) -> dict[str, object]:
    """Resolve only explicitly supported evidence kinds and audit every successful read."""
    if not getattr(actor, "has_perm", lambda _permission: False)(
        "moderation.view_sensitive_evidence"
    ):
        raise PermissionDenied("Sensitive evidence permission required")
    if not reason.strip():
        raise ValidationError("Evidence access reason is required")
    now = now or timezone.now()
    with transaction.atomic():
        evidence = ModerationEvidence.objects.select_related("case").get(pk=evidence_id)
        case = evidence.case
        if case.status == ModerationCase.Status.CLOSED and (
            case.evidence_retain_until is None or case.evidence_retain_until <= now
        ):
            result: dict[str, object] = {"available": False, "reason": "retention_expired"}
        elif evidence.kind == ModerationEvidence.Kind.MANUAL_NOTE:
            result = {"available": True, "kind": evidence.kind, "content": evidence.manual_note}
        elif evidence.kind == ModerationEvidence.Kind.NETWORK_ACTIVITY:
            activity = NetworkActivity.objects.filter(pk=evidence.object_id).first()
            result = (
                {"available": False, "reason": "purged"}
                if activity is None or not activity.text
                else {"available": True, "kind": evidence.kind, "content": activity.text}
            )
        else:
            # Membership/risk evidence is intentionally metadata-only in this viewer.
            result = {
                "available": evidence.object_id is not None,
                "kind": evidence.kind,
                "object_id": evidence.object_id,
            }
        ModerationEvidenceAccess.objects.create(
            evidence=evidence,
            actor=actor,
            reason=reason,
        )
        return result


@transaction.atomic
def add_manual_note(
    *, case_id: int, note: str, reason: str, staff_actor: User
) -> ModerationEvidence:
    if not staff_actor.has_perm("moderation.apply_moderation_actions"):
        raise PermissionDenied("Moderation action permission required")
    if not note.strip() or len(note) > 4000 or not reason.strip():
        raise ValidationError("A bounded note and operator reason are required")
    case = ModerationCase.objects.select_for_update().get(pk=case_id)
    evidence = ModerationEvidence.objects.create(
        case=case,
        kind=ModerationEvidence.Kind.MANUAL_NOTE,
        manual_note=note,
        verified_server_observation=False,
    )
    from apps.notifications.models import OperatorAuditEvent

    OperatorAuditEvent.objects.create(
        actor=staff_actor,
        action="MODERATION_MANUAL_NOTE_ADDED",
        target_type="ModerationCase",
        target_id=str(case.public_id),
        reason=reason,
        after_state={"evidence_id": evidence.pk, "kind": evidence.kind},
    )
    return evidence
