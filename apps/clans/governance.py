import logging

# mypy: disable-error-code="misc"
from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.clans.models import (
    ClanExpectedRemoval,
    ClanGovernanceCase,
    ClanGovernanceElector,
    ClanGovernanceVote,
    ClanMembership,
    ClanMembershipEvent,
    ClanRole,
    ClanSeason,
    ClanSeasonEvent,
)
from apps.clans.services import ClanConflict, require_interactive_clan_season
from apps.game_config.services import ClanConfig, resolve_game_config_version
from apps.players.models import Player
from apps.seasons.models import Season

logger = logging.getLogger(__name__)
REMOVAL_REASONS = {
    "ABUSIVE_BEHAVIOR",
    "SPAM",
    "HARASSMENT",
    "SABOTAGE",
    "COMMUNITY_REASON",
    "OTHER",
}


def _event(
    cs: ClanSeason,
    kind: str,
    at: datetime,
    *,
    source_type: str = "",
    source_id: str = "",
    metadata: dict[str, object] | None = None,
) -> None:
    ClanSeasonEvent.objects.get_or_create(
        clan_season=cs,
        kind=kind,
        source_type=source_type,
        source_id=source_id,
        defaults={"occurred_at": at, "metadata": metadata or {}},
    )


def _policy(cs: ClanSeason) -> ClanConfig:
    policy = resolve_game_config_version(cs.config_version_id).clan
    if policy is None:
        raise ClanConflict("Clan policy unavailable")
    return policy


def _season_allows(case_type: str, cs: ClanSeason, now: datetime) -> None:
    if (
        cs.season.status != Season.Status.ACTIVE
        or not cs.season.starts_at <= now < cs.season.ends_at
    ):
        raise ClanConflict("Clan Season is unavailable for governance")
    expected = (
        ClanSeason.State.RECOVERY
        if case_type == ClanGovernanceCase.Type.RECOVERY_DISSOLUTION
        else ClanSeason.State.ACTIVE
    )
    if cs.state != expected:
        raise ClanConflict("Clan governance is unavailable in its current state")


def _schedule_next(membership: ClanMembership, policy: ClanConfig) -> None:
    if membership.ended_at is not None or membership.last_clan_active_at is None:
        membership.next_inactivity_check_at = None
    elif membership.role == ClanRole.CAPTAIN:
        days = (
            policy.captain_recovery_days
            if membership.inactivity_warned_at
            else policy.captain_warning_days
        )
        membership.next_inactivity_check_at = membership.last_clan_active_at + timedelta(days=days)
    else:
        days = (
            policy.soldier_removal_days
            if membership.inactivity_warned_at
            else policy.soldier_warning_days
        )
        membership.next_inactivity_check_at = membership.last_clan_active_at + timedelta(days=days)


def record_clan_activity(membership_id: int, *, occurred_at: datetime) -> ClanMembership:
    """Lock order is ClanSeason then membership across recovery/opening paths."""
    identity = ClanMembership.objects.values("clan_season_id").get(pk=membership_id)
    with transaction.atomic():
        cs = ClanSeason.objects.select_for_update().get(pk=identity["clan_season_id"])
        membership = ClanMembership.objects.select_for_update().get(pk=membership_id)
        if membership.ended_at is not None:
            return membership
        if membership.last_clan_active_at is None or occurred_at > membership.last_clan_active_at:
            membership.last_clan_active_at = occurred_at
            membership.inactivity_warned_at = None
            _schedule_next(membership, _policy(cs))
            membership.save(
                update_fields=(
                    "last_clan_active_at",
                    "inactivity_warned_at",
                    "next_inactivity_check_at",
                )
            )
        if (
            membership.role == ClanRole.CAPTAIN
            and cs.state == ClanSeason.State.RECOVERY
            and cs.bot_admin_lost_at is None
            and cs.recovery_governance_opened_at is None
        ):
            cs.state, cs.recovery_reason, cs.recovery_started_at = ClanSeason.State.ACTIVE, "", None
            cs.save(update_fields=("state", "recovery_reason", "recovery_started_at", "updated_at"))
            _event(
                cs,
                "RECOVERY_CANCELLED",
                occurred_at,
                source_type="ACTIVITY",
                source_id=str(membership.pk),
            )
        return membership


def clan_home_integrity_available(cs: ClanSeason) -> bool:
    return cs.state == ClanSeason.State.ACTIVE and cs.bot_admin_lost_at is None


def _close_membership(
    membership: ClanMembership, reason: str, now: datetime, *, source_type: str, source_id: str
) -> None:
    if membership.ended_at is not None:
        return
    ClanExpectedRemoval.objects.get_or_create(
        membership=membership,
        defaults={"reason": reason, "source_type": source_type, "source_id": source_id},
    )
    membership.ended_at, membership.end_reason, membership.next_inactivity_check_at = (
        now,
        reason,
        None,
    )
    membership.save(update_fields=("ended_at", "end_reason", "next_inactivity_check_at"))
    ClanMembershipEvent.objects.create(
        membership=membership,
        kind=reason,
        occurred_at=now,
        metadata={"source_type": source_type, "source_id": source_id},
    )


def _electorate(cs: ClanSeason, target_player_id: int | None = None) -> QuerySet[ClanMembership]:
    rows = ClanMembership.objects.filter(clan_season=cs, ended_at__isnull=True).exclude(
        player__moderation_state__in=[
            Player.ModerationState.SUSPENDED,
            Player.ModerationState.BANNED,
        ]
    )
    return rows.exclude(player_id=target_player_id) if target_player_id is not None else rows


def open_case(
    *,
    initiator: Player,
    clan_season: ClanSeason,
    case_type: str,
    reason: str,
    target: ClanMembership | None = None,
    now: datetime | None = None,
) -> ClanGovernanceCase:
    now = now or timezone.now()
    if case_type not in ClanGovernanceCase.Type.values:
        raise ClanConflict("Invalid governance case type")
    if case_type == ClanGovernanceCase.Type.SOLDIER_REMOVAL and reason not in REMOVAL_REASONS:
        raise ClanConflict("Invalid removal reason")
    target_id = target.pk if target else None
    with transaction.atomic():
        cs = ClanSeason.objects.select_for_update().select_related("season").get(pk=clan_season.pk)
        _season_allows(case_type, cs, now)
        if initiator.pk != cs.captain_id:
            raise PermissionDenied("Captain required")
        locked_target = None
        if case_type == ClanGovernanceCase.Type.SOLDIER_REMOVAL:
            if target_id is None:
                raise ClanConflict("Target must be a current Soldier")
            locked_target = (
                ClanMembership.objects.select_for_update()
                .filter(pk=target_id, clan_season=cs, role=ClanRole.SOLDIER, ended_at__isnull=True)
                .first()
            )
            if locked_target is None:
                raise ClanConflict("Target must be a current Soldier")
        existing = ClanGovernanceCase.objects.filter(
            clan_season=cs, case_type=case_type, status=ClanGovernanceCase.Status.OPEN
        ).first()
        if existing:
            if (
                existing.initiator_id == initiator.pk
                and existing.target_membership_id == target_id
                and existing.reason == reason
            ):
                return existing
            raise ClanConflict("A conflicting governance case is open")
        policy = _policy(cs)
        voters = list(
            _electorate(cs, locked_target.player_id if locked_target else None).select_for_update()
        )
        if len(voters) < policy.governance_minimum_voters:
            raise ClanConflict("Insufficient eligible voters")
        try:
            case = ClanGovernanceCase.objects.create(
                clan_season=cs,
                initiator=initiator,
                target_membership=locked_target,
                case_type=case_type,
                reason=reason,
                opened_at=now,
                voting_deadline=now + timedelta(hours=policy.governance_vote_hours),
                config_version_id=cs.config_version_id,
                quorum_bps=policy.governance_quorum_bps,
                approval_bps=policy.governance_approval_bps,
                minimum_eligible_voters=policy.governance_minimum_voters,
                eligible_count=len(voters),
            )
        except IntegrityError as exc:
            raise ClanConflict("A conflicting governance case is open") from exc
        ClanGovernanceElector.objects.bulk_create(
            [ClanGovernanceElector(case=case, player_id=v.player_id, membership=v) for v in voters]
        )
        if case_type == ClanGovernanceCase.Type.RECOVERY_DISSOLUTION:
            cs.recovery_governance_opened_at = now
            cs.save(update_fields=("recovery_governance_opened_at", "updated_at"))
        _event(
            cs,
            "GOVERNANCE_OPENED",
            now,
            source_type="GOVERNANCE",
            source_id=str(case.pk),
            metadata={"case_type": case_type},
        )
        from apps.notifications.services import emit_notification_safely

        recipients = {row.player_id: row.player for row in voters}
        if locked_target:
            recipients[locked_target.player_id] = locked_target.player
        notification_recipients = tuple(recipients.values())

        def notify_opened() -> None:
            for recipient in notification_recipients:
                emit_notification_safely(
                    recipient=recipient,
                    event_key="CLAN_GOVERNANCE_OPENED",
                    source_domain="CLAN",
                    source_type="CLAN_GOVERNANCE_CASE",
                    source_id=str(case.pk),
                    idempotency_key=f"clan-governance-opened:{case.pk}:{recipient.pk}",
                    payload={"case_id": case.pk, "case_type": case.case_type},
                    telegram=True,
                )

        transaction.on_commit(notify_opened)
        return case


def cast_vote(
    *, case_id: int, voter: Player, choice: str, now: datetime | None = None
) -> ClanGovernanceVote:
    now = now or timezone.now()
    if choice not in ClanGovernanceVote.Choice.values:
        raise ClanConflict("Invalid vote")
    identity = ClanGovernanceCase.objects.values("clan_season_id").get(pk=case_id)
    with transaction.atomic():
        ClanSeason.objects.select_for_update().get(pk=identity["clan_season_id"])
        case = (
            ClanGovernanceCase.objects.select_for_update()
            .select_related("clan_season__season")
            .get(pk=case_id)
        )
        if case.status != ClanGovernanceCase.Status.OPEN or now >= case.voting_deadline:
            raise ClanConflict("Voting is closed")
        _season_allows(case.case_type, case.clan_season, now)
        if not ClanGovernanceElector.objects.filter(case=case, player=voter).exists():
            raise PermissionDenied("Voter is not in the electorate snapshot")
        try:
            return ClanGovernanceVote.objects.create(
                case=case, voter=voter, choice=choice, cast_at=now
            )
        except IntegrityError as exc:
            raise ClanConflict("Vote already cast") from exc


def _dissolve(case: ClanGovernanceCase, now: datetime) -> None:
    cs = ClanSeason.objects.select_for_update().get(pk=case.clan_season_id)
    if cs.state == ClanSeason.State.ENDED:
        return
    for membership in ClanMembership.objects.select_for_update().filter(
        clan_season=cs, ended_at__isnull=True
    ):
        _close_membership(
            membership,
            ClanMembership.EndReason.CLAN_DISBAND_RELEASE,
            now,
            source_type="GOVERNANCE",
            source_id=str(case.pk),
        )
    cs.state, cs.accepting_members = ClanSeason.State.ENDED, False
    cs.save(update_fields=("state", "accepting_members", "updated_at"))
    _event(cs, "CLAN_SEASON_DISSOLVED", now, source_type="GOVERNANCE", source_id=str(case.pk))


def finalize_case(case_id: int, *, now: datetime | None = None) -> ClanGovernanceCase:
    now = now or timezone.now()
    identity = ClanGovernanceCase.objects.values("clan_season_id").get(pk=case_id)
    with transaction.atomic():
        ClanSeason.objects.select_for_update().get(pk=identity["clan_season_id"])
        case = ClanGovernanceCase.objects.select_for_update().get(pk=case_id)
        if case.status != ClanGovernanceCase.Status.OPEN:
            return case
        if now < case.voting_deadline:
            raise ClanConflict("Governance case is not due")
        votes = list(case.votes.values_list("choice", flat=True))
        yes, no = (
            votes.count(ClanGovernanceVote.Choice.YES),
            votes.count(ClanGovernanceVote.Choice.NO),
        )
        cast = yes + no
        quorum = cast * 10000 >= case.eligible_count * case.quorum_bps
        approved = quorum and cast > 0 and yes * 10000 >= cast * case.approval_bps
        result_reason = (
            "APPROVED" if approved else "NO_QUORUM" if not quorum else "APPROVAL_THRESHOLD_NOT_MET"
        )
        case.yes_count, case.no_count, case.valid_cast_count = yes, no, cast
        case.quorum_met, case.approval_met, case.status, case.finalized_at = (
            quorum,
            approved,
            ClanGovernanceCase.Status.APPROVED if approved else ClanGovernanceCase.Status.REJECTED,
            now,
        )
        case.result_reason = result_reason
        case.save(
            update_fields=(
                "yes_count",
                "no_count",
                "valid_cast_count",
                "quorum_met",
                "approval_met",
                "status",
                "finalized_at",
                "result_reason",
            )
        )
        if (
            approved
            and case.case_type == ClanGovernanceCase.Type.SOLDIER_REMOVAL
            and case.target_membership_id
        ):
            target = ClanMembership.objects.select_for_update().get(pk=case.target_membership_id)
            _close_membership(
                target,
                ClanMembership.EndReason.GOVERNANCE_REMOVAL,
                now,
                source_type="GOVERNANCE",
                source_id=str(case.pk),
            )
        elif approved:
            _dissolve(case, now)
        case.execution_completed_at = now
        case.save(update_fields=("execution_completed_at",))
        _event(
            case.clan_season,
            "GOVERNANCE_FINALIZED",
            now,
            source_type="GOVERNANCE",
            source_id=str(case.pk),
            metadata={"status": case.status},
        )
        from apps.notifications.services import emit_notification_safely

        recipient_ids = set(case.electors.values_list("player_id", flat=True))
        if case.target_membership_id:
            assert case.target_membership is not None
            recipient_ids.add(case.target_membership.player_id)
        recipients = tuple(Player.objects.filter(pk__in=recipient_ids))

        def notify_finalized() -> None:
            for recipient in recipients:
                emit_notification_safely(
                    recipient=recipient,
                    event_key="CLAN_GOVERNANCE_FINALIZED",
                    source_domain="CLAN",
                    source_type="CLAN_GOVERNANCE_CASE",
                    source_id=str(case.pk),
                    idempotency_key=f"clan-governance-finalized:{case.pk}:{recipient.pk}",
                    payload={"case_id": case.pk, "status": case.status},
                    telegram=True,
                )

        transaction.on_commit(notify_finalized)
        return case


def request_dissolution(
    captain: Player, clan_season: ClanSeason, *, now: datetime | None = None
) -> ClanGovernanceCase | None:
    now = now or timezone.now()
    with transaction.atomic():
        cs = ClanSeason.objects.select_for_update().select_related("season").get(pk=clan_season.pk)
        require_interactive_clan_season(cs, at=now)
        if captain.pk != cs.captain_id:
            raise PermissionDenied("Captain required")
        members = list(
            ClanMembership.objects.select_for_update()
            .filter(clan_season=cs, ended_at__isnull=True)
            .order_by("pk")
        )
        if len(members) != 1 or members[0].player_id != captain.pk:
            return open_case(
                initiator=captain,
                clan_season=cs,
                case_type=ClanGovernanceCase.Type.CAPTAIN_DISSOLUTION,
                reason="CAPTAIN_REQUEST",
                now=now,
            )
        _close_membership(
            members[0],
            ClanMembership.EndReason.CLAN_DISBAND_RELEASE,
            now,
            source_type="CAPTAIN_DISSOLUTION",
            source_id=str(cs.pk),
        )
        cs.state, cs.accepting_members = ClanSeason.State.ENDED, False
        cs.save(update_fields=("state", "accepting_members", "updated_at"))
        _event(
            cs,
            "CLAN_SEASON_DISSOLVED",
            now,
            source_type="CAPTAIN_DISSOLUTION",
            source_id=str(cs.pk),
        )
        return None


def sweep_inactivity(*, now: datetime | None = None, limit: int = 100) -> int:
    now = now or timezone.now()
    ids = list(
        ClanMembership.objects.filter(ended_at__isnull=True, next_inactivity_check_at__lte=now)
        .order_by("next_inactivity_check_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    processed = 0
    for membership_id in ids:
        try:
            identity = ClanMembership.objects.values("clan_season_id").get(pk=membership_id)
            with transaction.atomic():
                cs = ClanSeason.objects.select_for_update().get(pk=identity["clan_season_id"])
                m = ClanMembership.objects.select_for_update().get(pk=membership_id)
                if (
                    m.ended_at is not None
                    or m.last_clan_active_at is None
                    or not m.next_inactivity_check_at
                    or m.next_inactivity_check_at > now
                ):
                    continue
                policy, age = _policy(cs), now - m.last_clan_active_at
                warning_days = (
                    policy.captain_warning_days
                    if m.role == ClanRole.CAPTAIN
                    else policy.soldier_warning_days
                )
                if age >= timedelta(days=warning_days) and m.inactivity_warned_at is None:
                    m.inactivity_warned_at = now
                    warning_event = ClanMembershipEvent.objects.create(
                        membership=m, kind="INACTIVITY_WARNING", occurred_at=now
                    )
                    from apps.notifications.services import emit_notification_safely

                    transaction.on_commit(
                        lambda player=m.player, event=warning_event: emit_notification_safely(
                            recipient=player,
                            event_key="CLAN_INACTIVITY_WARNING",
                            source_domain="CLAN",
                            source_type="CLAN_MEMBERSHIP_EVENT",
                            source_id=str(event.pk),
                            idempotency_key=f"clan-inactivity-warning:{event.pk}",
                            telegram=True,
                        )
                    )
                if m.role == ClanRole.SOLDIER and age >= timedelta(
                    days=policy.soldier_removal_days
                ):
                    _close_membership(
                        m,
                        ClanMembership.EndReason.INACTIVITY_REMOVAL,
                        now,
                        source_type="INACTIVITY",
                        source_id=str(m.pk),
                    )
                elif m.role == ClanRole.CAPTAIN and age >= timedelta(
                    days=policy.captain_recovery_days
                ):
                    if cs.state == ClanSeason.State.ACTIVE:
                        (
                            cs.state,
                            cs.recovery_reason,
                            cs.recovery_started_at,
                            cs.recovery_governance_opened_at,
                        ) = (
                            ClanSeason.State.RECOVERY,
                            "CAPTAIN_INACTIVITY",
                            now,
                            None,
                        )
                        cs.save(
                            update_fields=(
                                "state",
                                "recovery_reason",
                                "recovery_started_at",
                                "recovery_governance_opened_at",
                                "updated_at",
                            )
                        )
                        _event(
                            cs,
                            "RECOVERY_ENTERED",
                            now,
                            source_type="INACTIVITY",
                            source_id=str(m.pk),
                        )
                        if _electorate(cs).count() >= policy.governance_minimum_voters:
                            open_case(
                                initiator=cs.captain,
                                clan_season=cs,
                                case_type=ClanGovernanceCase.Type.RECOVERY_DISSOLUTION,
                                reason="CAPTAIN_INACTIVITY",
                                now=now,
                            )
                    m.next_inactivity_check_at = None
                else:
                    _schedule_next(m, policy)
                if m.ended_at is None:
                    m.save(update_fields=("inactivity_warned_at", "next_inactivity_check_at"))
                processed += 1
        except Exception:
            logger.exception("clan_inactivity_item_failed", extra={"membership_id": membership_id})
    return processed


def observe_bot_admin_state(
    clan_season_id: int, *, is_admin: bool, authoritative: bool, observed_at: datetime | None = None
) -> ClanSeason:
    observed_at = observed_at or timezone.now()
    if not authoritative:
        return ClanSeason.objects.get(pk=clan_season_id)
    with transaction.atomic():
        cs = ClanSeason.objects.select_for_update().get(pk=clan_season_id)
        policy = _policy(cs)
        if not is_admin and cs.bot_admin_lost_at is None:
            cs.bot_admin_lost_at, cs.bot_admin_restore_deadline, cs.recovery_reason = (
                observed_at,
                observed_at + timedelta(hours=policy.bot_admin_restore_grace_hours),
                "BOT_ADMIN_LOSS",
            )
            cs.save(
                update_fields=(
                    "bot_admin_lost_at",
                    "bot_admin_restore_deadline",
                    "recovery_reason",
                    "updated_at",
                )
            )
            _event(
                cs,
                "BOT_ADMIN_LOST",
                observed_at,
                source_type="TELEGRAM",
                source_id=str(int(observed_at.timestamp())),
            )
            from apps.notifications.services import emit_notification_safely

            transaction.on_commit(
                lambda captain=cs.captain, clan_season=cs: emit_notification_safely(
                    recipient=captain,
                    event_key="CLAN_BOT_ADMIN_LOST",
                    source_domain="CLAN",
                    source_type="CLAN_SEASON",
                    source_id=str(clan_season.pk),
                    idempotency_key=(
                        f"clan-bot-admin-lost:{clan_season.pk}:{int(observed_at.timestamp())}"
                    ),
                    telegram=True,
                )
            )
        elif is_admin and cs.bot_admin_lost_at is not None:
            cs.bot_admin_lost_at = cs.bot_admin_restore_deadline = None
            if cs.recovery_governance_opened_at is None and cs.recovery_reason == "BOT_ADMIN_LOSS":
                cs.recovery_reason, cs.recovery_started_at = "", None
                if cs.state == ClanSeason.State.RECOVERY:
                    cs.state = ClanSeason.State.ACTIVE
                    _event(
                        cs,
                        "RECOVERY_CANCELLED",
                        observed_at,
                        source_type="TELEGRAM",
                        source_id=str(int(observed_at.timestamp())),
                    )
            cs.save(
                update_fields=(
                    "bot_admin_lost_at",
                    "bot_admin_restore_deadline",
                    "recovery_reason",
                    "recovery_started_at",
                    "state",
                    "updated_at",
                )
            )
            _event(
                cs,
                "BOT_ADMIN_RESTORED",
                observed_at,
                source_type="TELEGRAM",
                source_id=str(int(observed_at.timestamp())),
            )
        return cs


def sweep_bot_admin_deadlines(*, now: datetime | None = None, limit: int = 100) -> int:
    now = now or timezone.now()
    ids = list(
        ClanSeason.objects.filter(
            state=ClanSeason.State.ACTIVE, bot_admin_restore_deadline__lte=now
        )
        .order_by("bot_admin_restore_deadline", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    count = 0
    for pk in ids:
        try:
            with transaction.atomic():
                cs = ClanSeason.objects.select_for_update().get(pk=pk)
                if (
                    cs.state != ClanSeason.State.ACTIVE
                    or not cs.bot_admin_restore_deadline
                    or cs.bot_admin_restore_deadline > now
                ):
                    continue
                cs.state, cs.recovery_started_at, cs.recovery_governance_opened_at = (
                    ClanSeason.State.RECOVERY,
                    now,
                    None,
                )
                cs.save(
                    update_fields=(
                        "state",
                        "recovery_started_at",
                        "recovery_governance_opened_at",
                        "updated_at",
                    )
                )
                _event(cs, "RECOVERY_ENTERED", now, source_type="BOT_ADMIN", source_id=str(pk))
                policy = _policy(cs)
                if _electorate(cs).count() >= policy.governance_minimum_voters:
                    open_case(
                        initiator=cs.captain,
                        clan_season=cs,
                        case_type=ClanGovernanceCase.Type.RECOVERY_DISSOLUTION,
                        reason="BOT_ADMIN_LOSS",
                        now=now,
                    )
                count += 1
        except Exception:
            logger.exception("clan_bot_admin_deadline_failed", extra={"clan_season_id": pk})
    return count
