from datetime import datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.clans.models import (
    ClanInvite,
    ClanJoinRequest,
    ClanMembership,
    ClanSeason,
    PlayerSeasonClanState,
)
from apps.clans.services import (
    cancel_request,
    choose_role,
    decide_request,
    eligible_groups,
    finalize_accepted_request,
    get_or_create_rejoin_invite,
    register_clan,
    request_join,
    voluntary_leave,
)
from apps.game_config.services import ResolvedGameConfig, resolve_effective_game_config
from apps.players.models import Player
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


def player(request: HttpRequest) -> Player:
    value = getattr(request, "auth", None)
    assert isinstance(value, Player)
    return value


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


def clan_out(row: ClanSeason) -> ClanOut:
    member_count = getattr(row, "current_member_count", None)
    if member_count is None:
        member_count = row.memberships.filter(ended_at__isnull=True).count()
    return ClanOut(
        id=row.clan_id,
        name=row.clan.name,
        tag=row.clan.tag,
        motto=row.clan.motto,
        member_count=member_count,
        capacity=row.base_capacity,
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
    return [clan_out(x) for x in rows[offset : offset + limit]]


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
