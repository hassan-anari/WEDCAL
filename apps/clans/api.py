from datetime import datetime
from typing import Literal, cast

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import Field

from apps.clans.governance import cast_vote, open_case, request_dissolution
from apps.clans.models import (
    ClanGovernanceCase,
    ClanInvite,
    ClanJoinRequest,
    ClanMembership,
    ClanSeason,
    PlayerSeasonClanState,
)
from apps.clans.services import (
    cancel_request,
    captain_cancel_accepted_request,
    choose_role,
    decide_request,
    eligible_groups,
    finalize_accepted_request,
    get_or_create_rejoin_invite,
    register_clan,
    request_join,
    voluntary_leave,
)
from apps.core.b3 import B3Error, b3_responses
from apps.game_config.services import (
    ResolvedGameConfig,
    resolve_effective_game_config,
    resolve_game_config_version,
)
from apps.players.models import Player
from apps.rewards.services import IdempotencyConflict
from apps.seasons.models import Season
from apps.seasons.services import NoActiveSeason, SeasonNotEligible, resolve_current_season
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["clans"], auth=mini_app_auth)


class RoleIn(Schema):
    role: Literal["CAPTAIN", "SOLDIER"]


class RoleOut(Schema):
    role: str


class GroupOut(Schema):
    id: int
    title: str
    username: str
    verification_state: Literal["CANDIDATE", "VERIFIED"] = "CANDIDATE"
    member_count: int | None = None
    image_url: str | None = None


class ClanCreateIn(Schema):
    telegram_group_id: int = Field(
        gt=0, description="Internal group ID from eligible-groups; never a raw Telegram chat ID"
    )
    name: str = Field(min_length=2, max_length=64)
    tag: str = Field(min_length=2, max_length=12)
    motto: str = Field(default="", max_length=160)


class ClanOut(Schema):
    id: int
    name: str
    tag: str
    motto: str
    member_count: int
    capacity: int
    accepting_members: bool
    rank: int | None = None
    level: int = 1
    score: int = 0
    state: str = "ACTIVE"
    image_url: str | None = None


class DecisionIn(Schema):
    accept: bool


class RequestOut(Schema):
    id: int
    state: str


class ApplicantOut(Schema):
    request_id: int
    first_name: str
    username: str
    level: int
    season_points: int
    state: str


class InviteOut(Schema):
    state: str
    invite_link: str | None
    delivery_state: str


class MembershipOut(Schema):
    membership_id: int
    clan_season_id: int
    access_rejoin_deadline: datetime | None
    clan_id: int
    clan_name: str
    role: str
    access_state: str
    started_at: datetime


class StateOut(Schema):
    enabled: bool
    eligible: bool
    role: str | None
    cooldown_until: datetime | None
    voluntary_leave_count: int
    unlock_level: int | None
    max_voluntary_leaves: int
    leave_cooldown_hours: int
    owner_decisions_pending: list[str] = ["PH-045", "PH-048"]


class RemovalCaseIn(Schema):
    target_membership_id: int
    reason: str


class VoteIn(Schema):
    choice: Literal["YES", "NO"]


class GovernanceOut(Schema):
    id: int
    case_type: str
    status: str
    voting_deadline: datetime
    quorum_bps: int
    approval_bps: int
    minimum_eligible_voters: int
    eligible_count: int
    reason: str
    target_membership_id: int | None
    target_first_name: str | None
    target_username: str | None
    is_eligible_voter: bool
    own_vote: str | None
    yes_count: int | None
    no_count: int | None
    valid_cast_count: int | None
    result: str | None


class RecoveryOut(Schema):
    state: str
    reason: str
    started_at: datetime | None
    bot_admin_restore_deadline: datetime | None
    home_integrity_available: bool


class ClanHomeOut(Schema):
    clan_id: int
    state: str
    hq_level: int
    hq_xp: int
    current_threshold: int
    next_threshold: int | None
    progress_to_next: int
    max_level: bool
    used_capacity: int
    accepted_reservations: int
    effective_capacity: int
    treasury_balance: int
    treasury_earned_total: int
    own_contribution: int
    total_contribution: int
    competitive_score: int


class ContributionOut(Schema):
    first_name: str
    username: str
    role: str
    contribution: int


class TreasuryEntryOut(Schema):
    direction: str
    amount: int
    source_type: str
    occurred_at: datetime


class TreasuryOut(Schema):
    balance: int
    earned_total: int
    history: list[TreasuryEntryOut]


class ShopItemOut(Schema):
    item_id: str
    name: str
    price: int
    activation: str
    available: bool


class IdempotencyIn(Schema):
    idempotency_key: str = Field(min_length=1, max_length=200)


class InventoryOut(Schema):
    item_id: str
    quantity: int


class MutationOut(Schema):
    id: int
    replayed: bool


class ClanMissionOut(Schema):
    key: str
    version: int
    kind: str
    target_units: int
    reward_band: str
    starts_at: datetime
    ends_at: datetime
    progress_units: int
    completed: bool
    requirement: str
    availability: Literal["AVAILABLE", "UNAVAILABLE"]
    blocked_reason: str | None
    participant_gross_points: int | None
    direct_hq_xp: int | None
    reward_state: str
    own_player_reward: int | None


def own_mission_reward(participation) -> int | None:  # type: ignore[no-untyped-def]
    if participation is None or participation.reward_settlement is None:
        return None
    return int(participation.reward_settlement.player_amount)


def player(request: HttpRequest) -> Player:
    value = getattr(request, "auth", None)
    assert isinstance(value, Player)
    return value


def governance_out(row: ClanGovernanceCase, viewer: Player) -> GovernanceOut:
    target_membership = row.target_membership if row.target_membership_id else None
    target = target_membership.player if target_membership is not None else None
    own_vote = row.votes.filter(voter=viewer).values_list("choice", flat=True).first()
    finalized = row.finalized_at is not None
    return GovernanceOut(
        id=row.pk,
        case_type=row.case_type,
        status=row.status,
        voting_deadline=row.voting_deadline,
        quorum_bps=row.quorum_bps,
        approval_bps=row.approval_bps,
        minimum_eligible_voters=row.minimum_eligible_voters,
        eligible_count=row.eligible_count,
        reason=row.reason,
        target_membership_id=row.target_membership_id,
        target_first_name=target.first_name if target else None,
        target_username=target.username if target else None,
        is_eligible_voter=row.electors.filter(player=viewer).exists(),
        own_vote=own_vote,
        yes_count=row.yes_count if finalized else None,
        no_count=row.no_count if finalized else None,
        valid_cast_count=row.valid_cast_count if finalized else None,
        result=row.result_reason if finalized else None,
    )


def context() -> tuple[Season, ResolvedGameConfig]:
    now = timezone.now()
    try:
        season = resolve_current_season(at=now)
    except (NoActiveSeason, SeasonNotEligible) as exc:
        raise B3Error("SEASON_UNAVAILABLE") from exc
    if season is None:
        raise B3Error("SEASON_UNAVAILABLE")
    return season, resolve_effective_game_config(now)


def fail(exc: Exception) -> B3Error:
    if isinstance(exc, PermissionDenied):
        return B3Error("CLAN_FORBIDDEN", 403)
    if isinstance(exc, IdempotencyConflict):
        return B3Error("IDEMPOTENCY_CONFLICT")
    if isinstance(
        exc, (ClanSeason.DoesNotExist, ClanMembership.DoesNotExist, ClanGovernanceCase.DoesNotExist)
    ):
        return B3Error("CLAN_NOT_FOUND", 404)
    if isinstance(exc, ValidationError) and any("PH-045" in message for message in exc.messages):
        return B3Error("CLAN_OWNER_DECISION_REQUIRED")
    return B3Error("CLAN_STATE_CONFLICT")


def clan_out(row: ClanSeason, policy=None) -> ClanOut:  # type: ignore[no-untyped-def]
    member_count = getattr(row, "current_member_count", None)
    if member_count is None:
        member_count = row.memberships.filter(ended_at__isnull=True).count()
    from apps.clans.economy import effective_capacity

    policy = policy or resolve_game_config_version(row.config_version_id).clan
    assert policy is not None
    from apps.clans.economy import hq_rule
    from apps.players.group_avatars import group_avatar_url

    return ClanOut(
        image_url=group_avatar_url(row.clan.telegram_group) or None,
        level=hq_rule(policy, row.hq_xp).level,
        score=row.competitive_score,
        state=row.state,
        id=row.clan_id,
        name=row.clan.name,
        tag=row.clan.tag,
        motto=row.clan.motto,
        member_count=member_count,
        capacity=effective_capacity(row, policy),
        accepting_members=row.accepting_members,
    )


@router.get("/state", response=b3_responses(StateOut))
def state(request: HttpRequest) -> StateOut:
    season, config = context()
    p = player(request)
    row = PlayerSeasonClanState.objects.filter(player=p, season=season).first()
    return StateOut(
        enabled=bool(config.clan and config.clan.enabled),
        eligible=bool(
            config.clan
            and getattr(p, "progression", None)
            and p.progression.level >= config.clan.min_level
        ),
        role=row.role if row else None,
        cooldown_until=row.leave_cooldown_until if row else None,
        voluntary_leave_count=row.voluntary_leave_count if row else 0,
        unlock_level=config.clan.min_level if config.clan else None,
        max_voluntary_leaves=config.clan.max_voluntary_leaves_per_season if config.clan else 0,
        leave_cooldown_hours=config.clan.leave_cooldown_hours if config.clan else 0,
    )


def current_membership(request: HttpRequest) -> ClanMembership:
    season, _ = context()
    return get_object_or_404(
        ClanMembership.objects.select_related("clan_season__season"),
        player=player(request),
        season=season,
        ended_at__isnull=True,
    )


@router.get("/home", response=b3_responses(ClanHomeOut))
def home(request: HttpRequest) -> ClanHomeOut:
    membership = current_membership(request)
    cs = membership.clan_season
    config = resolve_game_config_version(cs.config_version_id)
    assert config.clan is not None
    from apps.clans.economy import effective_capacity, hq_rule

    rule = hq_rule(config.clan, cs.hq_xp)
    later = [x for x in config.clan.hq_curve if x.level > rule.level]
    return ClanHomeOut(
        clan_id=cs.clan_id,
        state=cs.state,
        hq_level=rule.level,
        hq_xp=cs.hq_xp,
        current_threshold=rule.min_xp,
        next_threshold=later[0].min_xp if later else None,
        progress_to_next=cs.hq_xp - rule.min_xp,
        max_level=not later,
        used_capacity=cs.memberships.filter(ended_at__isnull=True).count(),
        accepted_reservations=cs.join_requests.filter(state=ClanJoinRequest.State.ACCEPTED).count(),
        effective_capacity=effective_capacity(cs, config.clan),
        treasury_balance=cs.treasury_balance,
        treasury_earned_total=cs.treasury_earned_total,
        own_contribution=membership.contribution_total,
        total_contribution=cs.contribution_total,
        competitive_score=cs.competitive_score,
    )


@router.get("/contributions", response=b3_responses(list[ContributionOut]))
def contributions(request: HttpRequest) -> list[ContributionOut]:
    cs = current_membership(request).clan_season
    return [
        ContributionOut(
            first_name=x.player.first_name,
            username=x.player.username,
            role=x.role,
            contribution=x.contribution_total,
        )
        for x in cs.memberships.select_related("player").order_by("-contribution_total")[:100]
    ]


@router.get("/treasury", response=b3_responses(TreasuryOut))
def treasury(request: HttpRequest) -> TreasuryOut:
    cs = current_membership(request).clan_season
    history = [
        TreasuryEntryOut(
            direction=x.direction,
            amount=x.amount,
            source_type=x.source_type,
            occurred_at=x.occurred_at,
        )
        for x in cs.treasury_entries.order_by("-occurred_at")[:50]
    ]
    return TreasuryOut(
        balance=cs.treasury_balance, earned_total=cs.treasury_earned_total, history=history
    )


@router.get("/shop", response=b3_responses(list[ShopItemOut]))
def shop(request: HttpRequest) -> list[ShopItemOut]:
    current_membership(request)
    _, config = context()
    assert config.clan is not None
    return [
        ShopItemOut(
            item_id=x.item_id,
            name=x.name,
            price=x.price,
            activation=x.activation or "UNAVAILABLE",
            available=x.available_s1,
        )
        for x in config.clan.inventory_items
        if x.available_s1
    ]


@router.post("/shop/{item_id}/purchase", response=b3_responses(MutationOut))
def purchase(request: HttpRequest, item_id: str, payload: IdempotencyIn) -> MutationOut:
    membership = current_membership(request)
    _, config = context()
    try:
        from apps.clans.economy import purchase_item

        row, created = purchase_item(
            clan_season=membership.clan_season,
            actor=player(request),
            item_id=item_id,
            idempotency_key=payload.idempotency_key,
            resolved_config=config,
        )
        return MutationOut(id=row.pk, replayed=not created)
    except (ValidationError, PermissionDenied, IdempotencyConflict) as exc:
        raise fail(exc) from exc


@router.get("/inventory", response=b3_responses(list[InventoryOut]))
def inventory(request: HttpRequest) -> list[InventoryOut]:
    return [
        InventoryOut(item_id=x.item_id, quantity=x.quantity)
        for x in current_membership(request).clan_season.inventory.order_by("item_id")
    ]


@router.post("/inventory/{item_id}/activate", response=b3_responses(MutationOut))
def activate(request: HttpRequest, item_id: str, payload: IdempotencyIn) -> MutationOut:
    membership = current_membership(request)
    _, config = context()
    try:
        from apps.clans.economy import activate_item

        row, created = activate_item(
            clan_season=membership.clan_season,
            actor=player(request),
            item_id=item_id,
            idempotency_key=payload.idempotency_key,
            resolved_config=config,
        )
        return MutationOut(id=row.pk, replayed=not created)
    except (ValidationError, PermissionDenied, IdempotencyConflict) as exc:
        raise fail(exc) from exc


@router.get("/missions", response=b3_responses(list[ClanMissionOut]))
def clan_missions(request: HttpRequest) -> list[ClanMissionOut]:
    membership = current_membership(request)
    from apps.clans.models import ClanMissionDefinition, ClanMissionProgress

    now = timezone.now()
    definitions = (
        ClanMissionDefinition.objects.filter(
            enabled=True,
            starts_at__lte=now,
            config_version_id=membership.clan_season.config_version_id,
        )
        .filter(
            models.Q(ends_at__gt=now)
            | models.Q(
                clanmissionprogress__clan_season=membership.clan_season,
                clanmissionprogress__completed_at__isnull=False,
            )
        )
        .distinct()
        .order_by("-ends_at", "pk")[:100]
    )
    progress = {
        x.definition_id: x
        for x in ClanMissionProgress.objects.filter(clan_season=membership.clan_season).filter(
            models.Q(membership=membership) | models.Q(membership__isnull=True)
        )
    }
    from apps.clans.missions import SUPPORTED_REQUIREMENTS

    policy = resolve_game_config_version(membership.clan_season.config_version_id).clan
    bands = {band.key: band for band in policy.mission_bands} if policy else {}
    from apps.clans.models import ClanMissionParticipant

    participations = {
        p.progress.definition_id: p
        for p in ClanMissionParticipant.objects.filter(
            membership=membership, progress__clan_season=membership.clan_season
        ).select_related("progress", "reward_settlement")
    }

    return [
        ClanMissionOut(
            key=row.key,
            version=row.version,
            kind=row.kind,
            target_units=row.target_units,
            reward_band=row.reward_band,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            progress_units=progress[row.pk].units if row.pk in progress else 0,
            completed=bool(row.pk in progress and progress[row.pk].completed_at),
            requirement=row.requirement,
            availability="AVAILABLE"
            if row.requirement in SUPPORTED_REQUIREMENTS
            else "UNAVAILABLE",
            blocked_reason=None
            if row.requirement in SUPPORTED_REQUIREMENTS
            else "MISSION_REQUIREMENT_UNSUPPORTED",
            participant_gross_points=bands[row.reward_band].participant_gross_points
            if row.reward_band in bands
            else None,
            direct_hq_xp=bands[row.reward_band].direct_hq_xp if row.reward_band in bands else None,
            reward_state=(
                "SETTLED"
                if row.pk in participations and participations[row.pk].reward_settlement_id
                else "INELIGIBLE"
                if row.pk in participations and participations[row.pk].reward_eligible is False
                else "NOT_PARTICIPATING"
                if row.pk in progress and progress[row.pk].completed_at
                else "NOT_COMPLETED"
            ),
            own_player_reward=own_mission_reward(participations.get(row.pk)),
        )
        for row in definitions
    ]


@router.post("/role", response=b3_responses(RoleOut))
def role(request: HttpRequest, payload: RoleIn) -> RoleOut:
    season, config = context()
    try:
        from apps.clans.services import require_eligible

        require_eligible(player(request), config)
        row = choose_role(player(request), season, payload.role)
        return RoleOut(role=row.role)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc


@router.get("/eligible-groups", response=b3_responses(list[GroupOut]))
def groups(request: HttpRequest) -> list[GroupOut]:
    season, config = context()
    try:
        return [
            GroupOut(id=x.pk, title=x.title, username=x.username)
            for x in eligible_groups(player(request), season, config)
        ]
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc


@router.post("/", response=b3_responses(ClanOut))
def create(request: HttpRequest, payload: ClanCreateIn) -> ClanOut:
    season, config = context()
    try:
        return clan_out(
            register_clan(
                player(request),
                season,
                config,
                group_id=payload.telegram_group_id,
                name=payload.name,
                tag=payload.tag,
                motto=payload.motto,
            )
        )
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc


@router.get("/", response=b3_responses(list[ClanOut]))
def discover(request: HttpRequest, q: str = "", limit: int = 20, offset: int = 0) -> list[ClanOut]:
    season, _ = context()
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    rows = (
        ClanSeason.objects.filter(season=season, state=ClanSeason.State.ACTIVE)
        .select_related("clan__telegram_group")
        .annotate(
            current_member_count=models.Count(
                "memberships", filter=models.Q(memberships__ended_at__isnull=True)
            )
        )
    )
    if q:
        rows = rows.filter(models.Q(clan__name__icontains=q) | models.Q(clan__tag__icontains=q))
    page = list(rows[offset : offset + limit])
    policies = {
        config_id: resolve_game_config_version(config_id).clan
        for config_id in {row.config_version_id for row in page}
    }
    return [clan_out(row, policies[row.config_version_id]) for row in page]


class OwnRequestOut(Schema):
    id: int
    clan_id: int
    clan_name: str
    state: str
    created_at: datetime
    decided_at: datetime | None
    invite_state: str | None
    delivery_state: str | None
    cancellation_pending: bool


@router.get("/join-requests/current", response=b3_responses(list[OwnRequestOut]))
def own_requests(request: HttpRequest, limit: int = 20) -> list[OwnRequestOut]:
    season, _ = context()
    rows = (
        ClanJoinRequest.objects.filter(applicant=player(request), clan_season__season=season)
        .select_related("clan_season__clan", "invite")
        .order_by("-pk")[: max(1, min(limit, 100))]
    )
    return [
        OwnRequestOut(
            id=r.pk,
            clan_id=r.clan_season.clan_id,
            clan_name=r.clan_season.clan.name,
            state=r.state,
            created_at=r.created_at,
            decided_at=r.decided_at,
            invite_state=r.invite.state if hasattr(r, "invite") else None,
            delivery_state=r.invite.delivery_state if hasattr(r, "invite") else None,
            cancellation_pending=bool(hasattr(r, "invite") and r.invite.desired_terminal_state),
        )
        for r in rows
    ]


@router.post("/join-requests/{request_id}/decision", response=b3_responses(RequestOut))
def decision(request: HttpRequest, request_id: int, payload: DecisionIn) -> RequestOut:
    try:
        row = decide_request(player(request), request_id, accept=payload.accept)
        if payload.accept:
            row = finalize_accepted_request(row)
        return RequestOut(id=row.pk, state=row.state)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc
    except ClanJoinRequest.DoesNotExist as exc:
        raise HttpError(404, "Join request not found") from exc


@router.delete("/join-requests/{request_id}", response=b3_responses(RequestOut))
def cancel(request: HttpRequest, request_id: int) -> RequestOut:
    try:
        row = cancel_request(player(request), request_id)
        return RequestOut(id=row.pk, state=row.state)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc
    except ClanJoinRequest.DoesNotExist as exc:
        raise HttpError(404, "Join request not found") from exc


@router.post("/join-requests/{request_id}/revoke", response=b3_responses(RequestOut))
def revoke_accepted(request: HttpRequest, request_id: int) -> RequestOut:
    try:
        row = captain_cancel_accepted_request(player(request), request_id)
        return RequestOut(id=row.pk, state=row.state)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc
    except ClanJoinRequest.DoesNotExist as exc:
        raise HttpError(404, "Join request not found") from exc


@router.get("/applicants", response=b3_responses(list[ApplicantOut]))
def applicants(request: HttpRequest, limit: int = 50) -> list[ApplicantOut]:
    season, _ = context()
    rows = (
        ClanJoinRequest.objects.filter(
            clan_season__captain=player(request),
            clan_season__season=season,
            state=ClanJoinRequest.State.PENDING,
        )
        .select_related("applicant__progression")
        .prefetch_related("applicant__seasons")[: min(max(limit, 1), 100)]
    )
    result = []
    for row in rows:
        season_projection = next(
            (x for x in row.applicant.seasons.all() if x.season_id == season.pk), None
        )
        result.append(
            ApplicantOut(
                request_id=row.pk,
                first_name=row.applicant.first_name,
                username=row.applicant.username,
                level=row.applicant.progression.level,
                season_points=season_projection.season_points if season_projection else 0,
                state=row.state,
            )
        )
    return result


@router.get("/invite/current", response=b3_responses(InviteOut))
def invite(request: HttpRequest) -> InviteOut:
    owner = player(request)
    row = (
        ClanInvite.objects.filter(
            models.Q(
                join_request__applicant=owner,
                join_request__state=ClanJoinRequest.State.ACCEPTED,
            )
            | models.Q(
                membership__player=owner,
                membership__ended_at__isnull=True,
                membership__access_state=ClanMembership.Access.ACCESS_LOST,
            )
        )
        .order_by("-created_at")
        .first()
    )
    if row is None:
        membership = ClanMembership.objects.filter(
            player=owner,
            ended_at__isnull=True,
            access_state=ClanMembership.Access.ACCESS_LOST,
        ).first()
        if membership is None:
            raise HttpError(404, "Invite unavailable")
        row = get_or_create_rejoin_invite(owner, membership)
    elif row.state in {ClanInvite.State.PENDING, ClanInvite.State.FAILED}:
        from apps.clans.services import create_personal_invite

        row = create_personal_invite(row)
    return InviteOut(
        state=row.state,
        invite_link=row.invite_link if row.state == ClanInvite.State.ACTIVE else None,
        delivery_state=row.delivery_state,
    )


@router.get(
    "/membership/current", response={**b3_responses(MembershipOut), 200: MembershipOut | None}
)
def membership(request: HttpRequest) -> MembershipOut | None:
    season, _ = context()
    row = (
        ClanMembership.objects.filter(player=player(request), season=season, ended_at__isnull=True)
        .select_related("clan_season__clan")
        .first()
    )
    return (
        None
        if row is None
        else MembershipOut(
            membership_id=row.pk,
            clan_season_id=row.clan_season_id,
            access_rejoin_deadline=row.access_rejoin_deadline,
            clan_id=row.clan_season.clan_id,
            clan_name=row.clan_season.clan.name,
            role=row.role,
            access_state=row.access_state,
            started_at=row.started_at,
        )
    )


@router.post("/leave", response={**b3_responses(StateOut), 204: None})
def leave(request: HttpRequest) -> tuple[int, None]:
    season, config = context()
    try:
        voluntary_leave(player(request), season, config)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc
    return 204, None


@router.get("/governance", response=b3_responses(list[GovernanceOut]))
def governance(request: HttpRequest) -> list[GovernanceOut]:
    season, _ = context()
    membership = ClanMembership.objects.filter(
        player=player(request), season=season, ended_at__isnull=True
    ).first()
    if membership is None:
        return []
    viewer = player(request)
    rows = (
        ClanGovernanceCase.objects.filter(clan_season=membership.clan_season)
        .select_related("target_membership__player")
        .order_by("-opened_at")[:100]
    )
    return [governance_out(row, viewer) for row in rows]


@router.post("/removal-cases", response=b3_responses(GovernanceOut))
def removal_case(request: HttpRequest, payload: RemovalCaseIn) -> GovernanceOut:
    season, _ = context()
    try:
        cs = ClanSeason.objects.get(
            captain=player(request), season=season, state=ClanSeason.State.ACTIVE
        )
        target = ClanMembership.objects.get(pk=payload.target_membership_id)
        row = open_case(
            initiator=player(request),
            clan_season=cs,
            case_type=ClanGovernanceCase.Type.SOLDIER_REMOVAL,
            reason=payload.reason,
            target=target,
        )
        return governance_out(row, player(request))
    except (
        ValidationError,
        PermissionDenied,
        ClanSeason.DoesNotExist,
        ClanMembership.DoesNotExist,
    ) as exc:
        raise fail(exc) from exc


@router.post("/governance/{case_id}/vote", response=b3_responses(GovernanceOut))
def vote(request: HttpRequest, case_id: int, payload: VoteIn) -> GovernanceOut:
    try:
        ballot = cast_vote(case_id=case_id, voter=player(request), choice=payload.choice)
        row = ballot.case
        return governance_out(row, player(request))
    except (ValidationError, PermissionDenied, ClanGovernanceCase.DoesNotExist) as exc:
        raise fail(exc) from exc


@router.post("/dissolution", response={**b3_responses(GovernanceOut), 204: None})
def dissolution(request: HttpRequest):  # type: ignore[no-untyped-def]
    season, _ = context()
    try:
        cs = ClanSeason.objects.get(
            captain=player(request), season=season, state=ClanSeason.State.ACTIVE
        )
        row = request_dissolution(player(request), cs)
        if row is None:
            return 204, None
        return 200, governance_out(row, player(request))
    except (ValidationError, PermissionDenied, ClanSeason.DoesNotExist) as exc:
        raise fail(exc) from exc


@router.get("/recovery", response=b3_responses(RecoveryOut))
def recovery(request: HttpRequest) -> RecoveryOut:
    from apps.clans.governance import clan_home_integrity_available

    season, _ = context()
    membership = get_object_or_404(
        ClanMembership, player=player(request), season=season, ended_at__isnull=True
    )
    cs = membership.clan_season
    return RecoveryOut(
        state=cs.state,
        reason=cs.recovery_reason,
        started_at=cs.recovery_started_at,
        bot_admin_restore_deadline=cs.bot_admin_restore_deadline,
        home_integrity_available=clan_home_integrity_available(cs),
    )


class RosterMemberOut(Schema):
    membership_id: int
    player_id: int
    first_name: str
    username: str
    level: int | None
    avatar_url: str
    role: Literal["CAPTAIN", "SOLDIER"]
    access_state: str
    contribution: int
    started_at: datetime
    own: bool


class RosterPage(Schema):
    items: list[RosterMemberOut]
    total_count: int
    next_offset: int | None


@router.get("/roster", response=b3_responses(RosterPage))
def roster(request: HttpRequest, offset: int = 0, limit: int = 50) -> RosterPage:
    viewer = player(request)
    cs = current_membership(request).clan_season
    offset, limit = max(0, offset), max(1, min(100, limit))
    query = (
        cs.memberships.filter(ended_at__isnull=True)
        .select_related("player__progression")
        .order_by("-contribution_total", "pk")
    )
    rows = list(query[offset : offset + limit + 1])
    from apps.players.avatars import avatar_url

    return RosterPage(
        items=[
            RosterMemberOut(
                membership_id=m.pk,
                player_id=m.player_id,
                first_name=m.player.first_name,
                username=m.player.username,
                level=m.player.progression.level if hasattr(m.player, "progression") else None,
                avatar_url=avatar_url(m.player, private_only=True),
                role=cast(Literal["CAPTAIN", "SOLDIER"], m.role),
                access_state=m.access_state,
                contribution=m.contribution_total,
                started_at=m.started_at,
                own=m.player_id == viewer.pk,
            )
            for m in rows[:limit]
        ],
        total_count=query.count(),
        next_offset=offset + limit if len(rows) > limit else None,
    )


class EffectRuleOut(Schema):
    category: str
    value: int


class ItemRuleOut(Schema):
    item_id: str
    name: str
    price: int
    activation: str | None
    duration_minutes: int | None
    effects: list[EffectRuleOut]
    quantity: int
    max_uses_per_season: int | None
    uses_this_season: int
    can_purchase: bool
    can_activate: bool
    blocked_reason: str | None


class EffectOut(Schema):
    id: int
    item_id: str
    category: str
    value: int
    starts_at: datetime
    expires_at: datetime
    effective: bool


class EquipmentOut(Schema):
    config_version: str
    server_time: datetime
    items: list[ItemRuleOut]
    active_effects: list[EffectOut]


@router.get("/equipment", response=b3_responses(EquipmentOut))
def equipment(request: HttpRequest) -> EquipmentOut:
    membership = current_membership(request)
    cs = membership.clan_season
    _, config = context()
    if config.clan is None:
        raise B3Error("CLAN_UNAVAILABLE")
    from apps.clans.economy import _captain, effective_capacity

    now = timezone.now()
    permitted = config.clan.enabled
    try:
        _captain(cs, player(request))
    except (PermissionDenied, ValidationError):
        permitted = False
    quantities = dict(cs.inventory.values_list("item_id", "quantity"))
    uses = {
        x["item_id"]: x["n"] for x in cs.item_uses.values("item_id").annotate(n=models.Count("id"))
    }
    items = []
    for item in config.clan.inventory_items:
        if not item.available_s1:
            continue
        count, used = quantities.get(item.item_id, 0), uses.get(item.item_id, 0)
        reason = None
        if not permitted:
            reason = "CAPTAIN_OR_CLAN_STATE_REQUIRED"
        elif count == 0:
            reason = "NOT_OWNED"
        elif item.max_uses_per_season is not None and used >= item.max_uses_per_season:
            reason = "SEASON_USE_LIMIT"
        elif any(
            e.category == "SEASON_CAPACITY_ADD"
            and effective_capacity(cs, config.clan) + e.value
            > config.clan.max_capacity_including_items
            for e in item.effects
        ):
            reason = "CAPACITY_LIMIT"
        items.append(
            ItemRuleOut(
                item_id=item.item_id,
                name=item.name,
                price=item.price,
                activation=item.activation,
                duration_minutes=item.duration_minutes,
                effects=[EffectRuleOut(category=e.category, value=e.value) for e in item.effects],
                quantity=count,
                max_uses_per_season=item.max_uses_per_season,
                uses_this_season=used,
                can_purchase=permitted and cs.treasury_balance >= item.price,
                can_activate=reason is None,
                blocked_reason=reason,
            )
        )
    effects = list(
        cs.active_effects.filter(starts_at__lte=now, expires_at__gt=now)
        .select_related("item_use")
        .order_by("category", "-value", "pk")
    )
    seen = set()
    output = []
    for effect in effects:
        output.append(
            EffectOut(
                id=effect.pk,
                item_id=effect.item_use.item_id,
                category=effect.category,
                value=effect.value,
                starts_at=effect.starts_at,
                expires_at=effect.expires_at,
                effective=effect.category not in seen,
            )
        )
        seen.add(effect.category)
    return EquipmentOut(
        config_version=config.version, server_time=now, items=items, active_effects=output
    )


class TrophyOut(Schema):
    key: str
    current: int | None
    target: int | None
    unlocked: bool
    unlocked_at: datetime | None
    available: bool
    blocked_reason: str | None = None


@router.get("/trophies", response=b3_responses(list[TrophyOut]))
def trophies(request: HttpRequest) -> list[TrophyOut]:
    cs = current_membership(request).clan_season
    policy = resolve_game_config_version(cs.config_version_id).clan
    if policy is None:
        raise B3Error("CLAN_UNAVAILABLE")
    unlocks = {r.key: r for r in cs.achievement_unlocks.all()}
    result = [
        TrophyOut(
            key="banner-raised",
            current=None,
            target=None,
            unlocked=False,
            unlocked_at=None,
            available=False,
            blocked_reason="PH-048_PRODUCT_DECISION_REQUIRED",
        )
    ]
    for key, current, target in [
        ("united-front", cs.contribution_total, policy.united_front_threshold),
        ("treasury-keeper", cs.treasury_earned_total, policy.treasury_keeper_threshold),
    ]:
        unlock = unlocks.get(key)
        result.append(
            TrophyOut(
                key=key,
                current=current,
                target=target,
                unlocked=bool(unlock),
                unlocked_at=unlock.unlocked_at if unlock else None,
                available=True,
            )
        )
    return result


@router.post("/eligible-groups/{group_id}/verify", response=b3_responses(GroupOut))
def verify_group_candidate(request: HttpRequest, group_id: int) -> GroupOut:
    season, config = context()
    try:
        candidate = eligible_groups(player(request), season, config).filter(pk=group_id).first()
        if candidate is None:
            raise PermissionDenied
        from apps.clans.services import _fresh_authority

        _fresh_authority(player(request), candidate)
        return GroupOut(
            id=candidate.pk,
            title=candidate.title,
            username=candidate.username,
            verification_state="VERIFIED",
        )
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc


# Ninja resolves routes in registration order. Keep dynamic identifiers last so
# static Phase 7A operations are never parsed as integer Clan identifiers.
@router.get("/{clan_id}", response=b3_responses(ClanOut))
def details(request: HttpRequest, clan_id: int) -> ClanOut:
    season, _ = context()
    return clan_out(
        get_object_or_404(
            ClanSeason.objects.select_related("clan__telegram_group"),
            clan_id=clan_id,
            season=season,
        )
    )


@router.post("/{clan_id}/join-requests", response=b3_responses(RequestOut))
def join(request: HttpRequest, clan_id: int) -> RequestOut:
    season, config = context()
    cs = get_object_or_404(ClanSeason, clan_id=clan_id, season=season)
    try:
        row = request_join(player(request), cs, config)
        return RequestOut(id=row.pk, state=row.state)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc
