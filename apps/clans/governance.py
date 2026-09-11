from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
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
)
from apps.clans.services import ClanConflict, require_interactive_clan_season
from apps.game_config.services import ClanConfig, resolve_game_config_version
from apps.players.models import Player


def record_clan_activity(membership_id: int, *, occurred_at: datetime) -> ClanMembership:
    """Server-only seam for meaningful, verified Clan activity (including future Phase 8)."""
    with transaction.atomic():
        membership = (
            ClanMembership.objects.select_for_update()
            .select_related("clan_season")
            .get(pk=membership_id)
        )
        if membership.ended_at is not None:
            return membership
        if membership.last_clan_active_at is None or occurred_at > membership.last_clan_active_at:
            membership.last_clan_active_at = occurred_at
            membership.inactivity_warned_at = None
            membership.save(update_fields=("last_clan_active_at", "inactivity_warned_at"))
        clan_season = membership.clan_season
        if (
            membership.role == ClanRole.CAPTAIN
            and clan_season.state == ClanSeason.State.RECOVERY
            and not clan_season.governance_cases.filter(
                status=ClanGovernanceCase.Status.OPEN
            ).exists()
            and clan_season.bot_admin_lost_at is None
        ):
            clan_season.state = ClanSeason.State.ACTIVE
            clan_season.recovery_reason = ""
            clan_season.recovery_started_at = None
            clan_season.save(
                update_fields=("state", "recovery_reason", "recovery_started_at", "updated_at")
            )
        return membership


def clan_home_integrity_available(clan_season: ClanSeason) -> bool:
    return clan_season.state == ClanSeason.State.ACTIVE and clan_season.bot_admin_lost_at is None


def _policy(clan_season: ClanSeason) -> ClanConfig:
    policy = resolve_game_config_version(clan_season.config_version_id).clan
    if policy is None:
        raise ClanConflict("Clan policy unavailable")
    return policy


def _close_membership(
    membership: ClanMembership, reason: str, now: datetime, *, source_type: str, source_id: str
) -> None:
    if membership.ended_at is not None:
        return
    ClanExpectedRemoval.objects.get_or_create(
        membership=membership,
        defaults={"reason": reason, "source_type": source_type, "source_id": source_id},
    )
    membership.ended_at, membership.end_reason = now, reason
    membership.save(update_fields=("ended_at", "end_reason"))
    ClanMembershipEvent.objects.create(
        membership=membership,
        kind=reason,
        occurred_at=now,
        metadata={"source_type": source_type, "source_id": source_id},
    )


def _electorate(clan_season: ClanSeason, target_id: int | None = None):  # type: ignore[no-untyped-def]
    rows = ClanMembership.objects.filter(clan_season=clan_season, ended_at__isnull=True).exclude(
        player__moderation_state__in=[
            Player.ModerationState.SUSPENDED,
            Player.ModerationState.BANNED,
        ]
    )
    return rows.exclude(player_id=target_id) if target_id is not None else rows


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
    if case_type == ClanGovernanceCase.Type.RECOVERY_DISSOLUTION:
        if (
            clan_season.state != ClanSeason.State.RECOVERY
            or clan_season.season.status != clan_season.season.Status.ACTIVE
            or not clan_season.season.starts_at <= now < clan_season.season.ends_at
        ):
            raise ClanConflict("Clan Recovery governance is unavailable")
    else:
        require_interactive_clan_season(clan_season, at=now)
    policy = _policy(clan_season)
    if (
        initiator.pk != clan_season.captain_id
        and case_type != ClanGovernanceCase.Type.RECOVERY_DISSOLUTION
    ):
        raise PermissionDenied("Captain required")
    if case_type == ClanGovernanceCase.Type.SOLDIER_REMOVAL and (
        target is None
        or target.clan_season_id != clan_season.pk
        or target.role != ClanRole.SOLDIER
        or target.ended_at is not None
    ):
        raise ClanConflict("Target must be a current Soldier")
    with transaction.atomic():
        clan_season = ClanSeason.objects.select_for_update().get(pk=clan_season.pk)
        existing = ClanGovernanceCase.objects.filter(
            clan_season=clan_season, case_type=case_type, status=ClanGovernanceCase.Status.OPEN
        ).first()
        if existing:
            return existing
        voters = list(_electorate(clan_season, target.player_id if target else None))
        if len(voters) < policy.governance_minimum_voters:
            raise ClanConflict("Insufficient eligible voters")
        try:
            case = ClanGovernanceCase.objects.create(
                clan_season=clan_season,
                initiator=initiator,
                target_membership=target,
                case_type=case_type,
                reason=reason,
                opened_at=now,
                voting_deadline=now + timedelta(hours=policy.governance_vote_hours),
                config_version_id=clan_season.config_version_id,
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
        ClanMembershipEvent.objects.create(
            membership=voters[0],
            kind="GOVERNANCE_OPENED",
            occurred_at=now,
            metadata={"case_id": case.pk, "case_type": case_type},
        )
        return case


def cast_vote(
    *, case_id: int, voter: Player, choice: str, now: datetime | None = None
) -> ClanGovernanceVote:
    now = now or timezone.now()
    if choice not in ClanGovernanceVote.Choice.values:
        raise ClanConflict("Invalid vote")
    with transaction.atomic():
        case = (
            ClanGovernanceCase.objects.select_for_update()
            .select_related("clan_season__season")
            .get(pk=case_id)
        )
        if case.status != ClanGovernanceCase.Status.OPEN or now >= case.voting_deadline:
            raise ClanConflict("Voting is closed")
        require_interactive_clan_season(case.clan_season, at=now)
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


def finalize_case(case_id: int, *, now: datetime | None = None) -> ClanGovernanceCase:
    now = now or timezone.now()
    with transaction.atomic():
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
        case.yes_count, case.no_count, case.valid_cast_count = yes, no, cast
        case.quorum_met, case.approval_met = quorum, approved
        case.status = (
            ClanGovernanceCase.Status.APPROVED if approved else ClanGovernanceCase.Status.REJECTED
        )
        case.finalized_at = now
        case.save(
            update_fields=(
                "yes_count",
                "no_count",
                "valid_cast_count",
                "quorum_met",
                "approval_met",
                "status",
                "finalized_at",
            )
        )
        if approved:
            if (
                case.case_type == ClanGovernanceCase.Type.SOLDIER_REMOVAL
                and case.target_membership_id
            ):
                target = ClanMembership.objects.select_for_update().get(
                    pk=case.target_membership_id
                )
                _close_membership(
                    target,
                    ClanMembership.EndReason.GOVERNANCE_REMOVAL,
                    now,
                    source_type="GOVERNANCE",
                    source_id=str(case.pk),
                )
            else:
                _dissolve(case, now)
        case.execution_completed_at = now
        case.save(update_fields=("execution_completed_at",))
        return case


def request_dissolution(
    captain: Player, clan_season: ClanSeason, *, now: datetime | None = None
) -> ClanGovernanceCase | None:
    now = now or timezone.now()
    if captain.pk != clan_season.captain_id:
        raise PermissionDenied("Captain required")
    if clan_season.memberships.filter(ended_at__isnull=True).count() > 1:
        return open_case(
            initiator=captain,
            clan_season=clan_season,
            case_type=ClanGovernanceCase.Type.CAPTAIN_DISSOLUTION,
            reason="CAPTAIN_REQUEST",
            now=now,
        )
    with transaction.atomic():
        membership = ClanMembership.objects.select_for_update().get(
            clan_season=clan_season, ended_at__isnull=True
        )
        _close_membership(
            membership,
            ClanMembership.EndReason.CLAN_DISBAND_RELEASE,
            now,
            source_type="CAPTAIN_DISSOLUTION",
            source_id=str(clan_season.pk),
        )
        cs = ClanSeason.objects.select_for_update().get(pk=clan_season.pk)
        cs.state, cs.accepting_members = ClanSeason.State.ENDED, False
        cs.save(update_fields=("state", "accepting_members", "updated_at"))
    return None


def sweep_inactivity(*, now: datetime | None = None, limit: int = 100) -> int:
    now = now or timezone.now()
    ids = list(
        ClanMembership.objects.filter(ended_at__isnull=True, last_clan_active_at__isnull=False)
        .order_by("last_clan_active_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    processed = 0
    for membership_id in ids:
        try:
            with transaction.atomic():
                m = (
                    ClanMembership.objects.select_for_update()
                    .select_related("clan_season")
                    .get(pk=membership_id)
                )
                if m.ended_at is not None or m.last_clan_active_at is None:
                    continue
                policy = _policy(m.clan_season)
                age = now - m.last_clan_active_at
                warning_days = (
                    policy.captain_warning_days
                    if m.role == ClanRole.CAPTAIN
                    else policy.soldier_warning_days
                )
                if age >= timedelta(days=warning_days) and m.inactivity_warned_at is None:
                    m.inactivity_warned_at = now
                    m.save(update_fields=("inactivity_warned_at",))
                    ClanMembershipEvent.objects.create(
                        membership=m, kind="INACTIVITY_WARNING", occurred_at=now
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
                    cs = ClanSeason.objects.select_for_update().get(pk=m.clan_season_id)
                    if cs.state == ClanSeason.State.ACTIVE:
                        cs.state, cs.recovery_reason, cs.recovery_started_at = (
                            ClanSeason.State.RECOVERY,
                            "CAPTAIN_INACTIVITY",
                            now,
                        )
                        cs.save(
                            update_fields=(
                                "state",
                                "recovery_reason",
                                "recovery_started_at",
                                "updated_at",
                            )
                        )
                        if _electorate(cs).count() >= policy.governance_minimum_voters:
                            open_case(
                                initiator=cs.captain,
                                clan_season=cs,
                                case_type=ClanGovernanceCase.Type.RECOVERY_DISSOLUTION,
                                reason="CAPTAIN_INACTIVITY",
                                now=now,
                            )
                processed += 1
        except Exception:
            continue
    return processed


def observe_bot_admin_state(
    clan_season_id: int, *, is_admin: bool, authoritative: bool, observed_at: datetime | None = None
) -> ClanSeason:
    """Persist only authoritative Telegram state; transport ambiguity is never loss proof."""
    observed_at = observed_at or timezone.now()
    if not authoritative:
        return ClanSeason.objects.get(pk=clan_season_id)
    with transaction.atomic():
        cs = ClanSeason.objects.select_for_update().get(pk=clan_season_id)
        policy = _policy(cs)
        if not is_admin and cs.bot_admin_lost_at is None:
            cs.bot_admin_lost_at = observed_at
            cs.bot_admin_restore_deadline = observed_at + timedelta(
                hours=policy.bot_admin_restore_grace_hours
            )
            cs.recovery_reason = "BOT_ADMIN_LOSS"
            cs.save(
                update_fields=(
                    "bot_admin_lost_at",
                    "bot_admin_restore_deadline",
                    "recovery_reason",
                    "updated_at",
                )
            )
        elif (
            is_admin
            and cs.bot_admin_lost_at is not None
            and not cs.governance_cases.filter(status=ClanGovernanceCase.Status.OPEN).exists()
        ):
            cs.bot_admin_lost_at = cs.bot_admin_restore_deadline = None
            if cs.recovery_reason == "BOT_ADMIN_LOSS":
                cs.recovery_reason = ""
                cs.recovery_started_at = None
                if cs.state == ClanSeason.State.RECOVERY:
                    cs.state = ClanSeason.State.ACTIVE
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
        with transaction.atomic():
            cs = ClanSeason.objects.select_for_update().get(pk=pk)
            if (
                cs.state == ClanSeason.State.ACTIVE
                and cs.bot_admin_restore_deadline
                and cs.bot_admin_restore_deadline <= now
            ):
                cs.state, cs.recovery_started_at = ClanSeason.State.RECOVERY, now
                cs.save(update_fields=("state", "recovery_started_at", "updated_at"))
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
    return count
