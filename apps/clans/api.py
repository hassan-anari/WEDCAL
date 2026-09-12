from datetime import datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

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
    role: str


class RoleOut(Schema):
    role: str


class GroupOut(Schema):
    id: int
    title: str
    username: str


class ClanCreateIn(Schema):
    telegram_group_id: int
    name: str
    tag: str
    motto: str = ""


class ClanOut(Schema):
    id: int
    name: str
    tag: str
    motto: str
    member_count: int
    capacity: int
    accepting_members: bool
    rank: int | None = None


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


class RemovalCaseIn(Schema):
    target_membership_id: int
    reason: str


class VoteIn(Schema):
    choice: str


class GovernanceOut(Schema):
    id: int
    case_type: str
    status: str
    voting_deadline: datetime
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
    idempotency_key: str


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
        raise HttpError(409, "No current Clan Season is available") from exc
    if season is None:
        raise HttpError(409, "No current Clan Season is available")
    return season, resolve_effective_game_config(now)


def fail(exc: Exception) -> HttpError:
    return HttpError(403 if isinstance(exc, PermissionDenied) else 409, str(exc))


def clan_out(row: ClanSeason, policy=None) -> ClanOut:  # type: ignore[no-untyped-def]
    member_count = getattr(row, "current_member_count", None)
    if member_count is None:
        member_count = row.memberships.filter(ended_at__isnull=True).count()
    from apps.clans.economy import effective_capacity

    policy = policy or resolve_game_config_version(row.config_version_id).clan
    assert policy is not None
    return ClanOut(
        id=row.clan_id,
        name=row.clan.name,
        tag=row.clan.tag,
        motto=row.clan.motto,
        member_count=member_count,
        capacity=effective_capacity(row, policy),
        accepting_members=row.accepting_members,
    )


@router.get("/state", response=StateOut)
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
    )


def current_membership(request: HttpRequest) -> ClanMembership:
    season, _ = context()
    return get_object_or_404(
        ClanMembership.objects.select_related("clan_season__season"),
        player=player(request),
        season=season,
        ended_at__isnull=True,
    )


@router.get("/home", response=ClanHomeOut)
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


@router.get("/contributions", response=list[ContributionOut])
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


@router.get("/treasury", response=TreasuryOut)
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


@router.get("/shop", response=list[ShopItemOut])
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


@router.post("/shop/{item_id}/purchase", response=MutationOut)
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


@router.get("/inventory", response=list[InventoryOut])
def inventory(request: HttpRequest) -> list[InventoryOut]:
    return [
        InventoryOut(item_id=x.item_id, quantity=x.quantity)
        for x in current_membership(request).clan_season.inventory.order_by("item_id")
    ]


@router.post("/inventory/{item_id}/activate", response=MutationOut)
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


@router.get("/missions", response=list[ClanMissionOut])
def clan_missions(request: HttpRequest) -> list[ClanMissionOut]:
    membership = current_membership(request)
    from apps.clans.models import ClanMissionDefinition, ClanMissionProgress

    now = timezone.now()
    definitions = ClanMissionDefinition.objects.filter(
        enabled=True,
        starts_at__lte=now,
        ends_at__gt=now,
        config_version_id=membership.clan_season.config_version_id,
    ).order_by("ends_at")[:100]
    progress = {
        x.definition_id: x
        for x in ClanMissionProgress.objects.filter(clan_season=membership.clan_season).filter(
            models.Q(membership=membership) | models.Q(membership__isnull=True)
        )
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
        )
        for row in definitions
    ]


@router.post("/role", response=RoleOut)
def role(request: HttpRequest, payload: RoleIn) -> RoleOut:
    season, config = context()
    try:
        from apps.clans.services import require_eligible

        require_eligible(player(request), config)
        row = choose_role(player(request), season, payload.role)
        return RoleOut(role=row.role)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc


@router.get("/eligible-groups", response=list[GroupOut])
def groups(request: HttpRequest) -> list[GroupOut]:
    season, config = context()
    try:
        return [
            GroupOut(id=x.pk, title=x.title, username=x.username)
            for x in eligible_groups(player(request), season, config)
        ]
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc


@router.post("/", response=ClanOut)
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


@router.get("/", response=list[ClanOut])
def discover(request: HttpRequest, q: str = "", limit: int = 20, offset: int = 0) -> list[ClanOut]:
    season, _ = context()
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    rows = (
        ClanSeason.objects.filter(season=season, state=ClanSeason.State.ACTIVE)
        .select_related("clan")
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


@router.post("/join-requests/{request_id}/decision", response=RequestOut)
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


@router.delete("/join-requests/{request_id}", response=RequestOut)
def cancel(request: HttpRequest, request_id: int) -> RequestOut:
    try:
        row = cancel_request(player(request), request_id)
        return RequestOut(id=row.pk, state=row.state)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc
    except ClanJoinRequest.DoesNotExist as exc:
        raise HttpError(404, "Join request not found") from exc


@router.post("/join-requests/{request_id}/revoke", response=RequestOut)
def revoke_accepted(request: HttpRequest, request_id: int) -> RequestOut:
    try:
        row = captain_cancel_accepted_request(player(request), request_id)
        return RequestOut(id=row.pk, state=row.state)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc
    except ClanJoinRequest.DoesNotExist as exc:
        raise HttpError(404, "Join request not found") from exc


@router.get("/applicants", response=list[ApplicantOut])
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


@router.get("/invite/current", response=InviteOut)
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


@router.get("/membership/current", response=MembershipOut | None)
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
            clan_id=row.clan_season.clan_id,
            clan_name=row.clan_season.clan.name,
            role=row.role,
            access_state=row.access_state,
            started_at=row.started_at,
        )
    )


@router.post("/leave", response={204: None})
def leave(request: HttpRequest) -> tuple[int, None]:
    season, config = context()
    try:
        voluntary_leave(player(request), season, config)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc
    return 204, None


@router.get("/governance", response=list[GovernanceOut])
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


@router.post("/removal-cases", response=GovernanceOut)
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


@router.post("/governance/{case_id}/vote", response=GovernanceOut)
def vote(request: HttpRequest, case_id: int, payload: VoteIn) -> GovernanceOut:
    try:
        ballot = cast_vote(case_id=case_id, voter=player(request), choice=payload.choice)
        row = ballot.case
        return governance_out(row, player(request))
    except (ValidationError, PermissionDenied, ClanGovernanceCase.DoesNotExist) as exc:
        raise fail(exc) from exc


@router.post("/dissolution", response={204: None, 200: GovernanceOut})
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


@router.get("/recovery", response=RecoveryOut)
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


# Ninja resolves routes in registration order. Keep dynamic identifiers last so
# static Phase 7A operations are never parsed as integer Clan identifiers.
@router.get("/{clan_id}", response=ClanOut)
def details(request: HttpRequest, clan_id: int) -> ClanOut:
    season, _ = context()
    return clan_out(
        get_object_or_404(ClanSeason.objects.select_related("clan"), clan_id=clan_id, season=season)
    )


@router.post("/{clan_id}/join-requests", response=RequestOut)
def join(request: HttpRequest, clan_id: int) -> RequestOut:
    season, config = context()
    cs = get_object_or_404(ClanSeason, clan_id=clan_id, season=season)
    try:
        row = request_join(player(request), cs, config)
        return RequestOut(id=row.pk, state=row.state)
    except (ValidationError, PermissionDenied) as exc:
        raise fail(exc) from exc
