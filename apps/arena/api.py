from datetime import datetime
from typing import Any, Literal

from django.db import models, transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema
from pydantic import Field

from apps.arena.epochs import player_dlx_balance
from apps.arena.models import RankingProjectionEvent
from apps.arena.services import (
    RankingProjectionUnavailable,
    RankingSlice,
    dispatch_rebuild_safely,
    read_ranking_slice,
)
from apps.clans.economy import effective_capacity
from apps.clans.models import ClanMembership, ClanSeason
from apps.core.b3 import B3Error, b3_responses
from apps.game_config.services import resolve_effective_game_config, resolve_game_config_version
from apps.players.avatars import avatar_url
from apps.players.models import Player
from apps.progression.services import calculate_progression
from apps.seasons.models import (
    ClanSeasonFinalStanding,
    PlayerSeason,
    PlayerSeasonFinalStanding,
    Season,
)
from apps.seasons.services import resolve_current_season
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["arena"], auth=mini_app_auth)


class PageQuery(Schema):
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1)
    season: str | None = Field(default=None, max_length=64)


class ClanIdentity(Schema):
    name: str
    tag: str


class PlayerRankingItem(Schema):
    rank: int
    player_id: int
    first_name: str
    username: str
    avatar_url: str
    level: int
    season_points: int
    clan: ClanIdentity | None


class ClanRankingItem(Schema):
    rank: int
    clan_id: int
    name: str
    tag: str
    crest: str | None
    hq_level: int
    member_count: int
    capacity: int
    competitive_score: int


class RankingPage(Schema):
    offset: int
    limit: int
    total_count: int
    next_offset: int | None
    season: str
    season_status: str
    archived: bool
    as_of: datetime


class PlayerRankings(RankingPage):
    items: list[PlayerRankingItem]
    own_standing: PlayerRankingItem | None


class ClanRankings(RankingPage):
    items: list[ClanRankingItem]
    own_standing: ClanRankingItem | None


class SeasonOutput(Schema):
    code: str
    title: str
    status: str
    starts_at: datetime
    ends_at: datetime


class WarfrontOutput(Schema):
    state: Literal["LOCKED"] = "LOCKED"
    available_season: Literal[2] = 2
    message: str = "Warfront arrives in Season 2"


class ArenaOutput(Schema):
    season: SeasonOutput | None
    seasons: list[SeasonOutput]
    warfront: WarfrontOutput
    wallet_enabled: Literal[False] = False
    account_link_enabled: Literal[False] = False


class DlxOutput(Schema):
    season: str
    confirmed: int
    held: int
    forfeited: int
    net_confirmed: int
    claimable: Literal[0] = 0
    claimable_enabled: Literal[False] = False
    transferable: Literal[False] = False
    tradable: Literal[False] = False
    on_chain: Literal[False] = False
    price_status: Literal["UNAVAILABLE"] = "UNAVAILABLE"


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    return player


def _season(code: str | None) -> Season:
    if code:
        season = Season.objects.filter(
            code=code, status__in=["ACTIVE", "CLOSED", "ARCHIVED"]
        ).first()
    else:
        season = resolve_current_season(at=timezone.now(), required=False)
        if season is None:
            season = (
                Season.objects.filter(status__in=["CLOSED", "ARCHIVED"])
                .order_by("-starts_at")
                .first()
            )
    if season is None:
        raise B3Error("SEASON_UNAVAILABLE")
    return season


def _page(query: PageQuery, policy: Any) -> tuple[int, int]:
    limit = policy.default_page_size if query.limit is None else query.limit
    if query.offset < 0 or limit < 1 or limit > policy.max_page_size:
        raise B3Error("REQUEST_INVALID", 422)
    return query.offset, limit


def _pending(season: Season, entity: str) -> None:
    if RankingProjectionEvent.objects.filter(
        season=season, entity_type=entity, processed_at__isnull=True
    ).exists():
        raise B3Error("RANKING_RECONCILING", 503, retry_after=2)


def _ranking(season: Season, entity: str, offset: int, limit: int, own: int | None) -> RankingSlice:
    _pending(season, entity)
    try:
        return read_ranking_slice(
            season_id=season.pk, entity_type=entity, offset=offset, limit=limit, own_entity_id=own
        )
    except RankingProjectionUnavailable as exc:
        raise B3Error("RANKING_UNAVAILABLE", 503, retry_after=2) from exc


def _validate(
    season: Season, entity: str, ranking: RankingSlice, rows: dict[int, Any], score: str
) -> None:
    for pk, value in ranking.scores.items():
        row = rows.get(pk)
        if (
            row is None
            or (entity == "CLAN" and row.state == ClanSeason.State.ENDED)
            or getattr(row, score) != value
            or row.ranking_generation != ranking.generations.get(pk)
        ):
            dispatch_rebuild_safely(season.pk, entity)
            raise B3Error("RANKING_RECONCILING", 503, retry_after=2)
    _pending(season, entity)


def _metadata(season: Season, offset: int, limit: int, total: int) -> dict[str, Any]:
    return {
        "offset": offset,
        "limit": limit,
        "total_count": total,
        "next_offset": offset + limit if offset + limit < total else None,
        "season": season.code,
        "season_status": season.status,
        "archived": season.status in {"CLOSED", "ARCHIVED"},
        "as_of": timezone.now(),
    }


@router.get("/rankings/players", response=b3_responses(PlayerRankings))
def player_rankings(request: HttpRequest, query: Query[PageQuery]) -> dict[str, Any]:
    player, season = _player(request), _season(query.season)
    config = resolve_game_config_version(season.baseline_config_id)
    offset, limit = _page(query, config.rankings)
    closed = season.status in {"CLOSED", "ARCHIVED"}
    final: dict[int, PlayerSeasonFinalStanding] = {}
    if closed:
        standings = PlayerSeasonFinalStanding.objects.filter(season=season)
        batch = list(standings.order_by("final_rank")[offset : offset + limit])
        own = standings.filter(player=player).first()
        final = {row.player_id: row for row in batch + ([own] if own else [])}
        ranked = [(row.player_id, row.final_rank) for row in batch]
        own_rank, total = (own.final_rank if own else None), standings.count()
    else:
        ranking = _ranking(season, "PLAYER", offset, limit, player.pk)
        ranked, own_rank, total = list(ranking.ranked_ids), ranking.own_rank, ranking.total_count
    ids = [pk for pk, _ in ranked] + ([player.pk] if own_rank else [])
    rows = {
        row.player_id: row
        for row in PlayerSeason.objects.filter(season=season, player_id__in=ids).select_related(
            "player", "player__progression"
        )
    }
    if not closed:
        _validate(season, "PLAYER", ranking, rows, "season_points")
    display_progression = resolve_effective_game_config(timezone.now()).progression
    memberships = {
        row.player_id: row
        for row in ClanMembership.objects.filter(
            player_id__in=ids, season=season, ended_at__isnull=True
        ).select_related("clan_season__clan")
    }

    def item(pk: int, rank: int) -> dict[str, Any]:
        row = rows.get(pk)
        if row is None:
            raise B3Error("RANKING_RECONCILING", 503)
        membership = memberships.get(pk)
        if not hasattr(row.player, "progression"):
            raise B3Error("RANKING_RECONCILING", 503, retry_after=2)
        points = row.player.progression.lifetime_points
        return {
            "rank": rank,
            "player_id": pk,
            "first_name": row.player.first_name,
            "username": row.player.username,
            "avatar_url": avatar_url(row.player),
            "level": calculate_progression(points, display_progression).level,
            "season_points": final[pk].final_season_points if closed else row.season_points,
            "clan": {
                "name": membership.clan_season.clan.name,
                "tag": membership.clan_season.clan.tag,
            }
            if membership
            else None,
        }

    return {
        **_metadata(season, offset, limit, total),
        "items": [item(pk, rank) for pk, rank in ranked],
        "own_standing": item(player.pk, own_rank) if own_rank else None,
    }


@router.get("/rankings/clans", response=b3_responses(ClanRankings))
def clan_rankings(request: HttpRequest, query: Query[PageQuery]) -> dict[str, Any]:
    player, season = _player(request), _season(query.season)
    config = resolve_game_config_version(season.baseline_config_id)
    offset, limit = _page(query, config.rankings)
    membership = ClanMembership.objects.filter(
        player=player, season=season, ended_at__isnull=True
    ).first()
    own_id = membership.clan_season_id if membership else None
    closed = season.status in {"CLOSED", "ARCHIVED"}
    final: dict[int, ClanSeasonFinalStanding] = {}
    if closed:
        standings = ClanSeasonFinalStanding.objects.filter(season=season)
        batch = list(standings.order_by("final_rank")[offset : offset + limit])
        own = standings.filter(clan_season_id=own_id).first() if own_id else None
        final = {row.clan_season_id: row for row in batch + ([own] if own else [])}
        ranked = [(row.clan_season_id, row.final_rank) for row in batch]
        own_rank, total = (own.final_rank if own else None), standings.count()
    else:
        ranking = _ranking(season, "CLAN", offset, limit, own_id)
        ranked, own_rank, total = list(ranking.ranked_ids), ranking.own_rank, ranking.total_count
    ids = [pk for pk, _ in ranked] + ([own_id] if own_rank and own_id else [])
    rows = {
        row.pk: row
        for row in ClanSeason.objects.filter(pk__in=ids, season=season)
        .select_related("clan")
        .annotate(
            member_count=models.Count(
                "memberships", filter=models.Q(memberships__ended_at__isnull=True)
            )
        )
    }
    if not closed:
        _validate(season, "CLAN", ranking, rows, "competitive_score")

    def item(pk: int, rank: int) -> dict[str, Any]:
        row = rows.get(pk)
        if row is None:
            raise B3Error("RANKING_RECONCILING", 503)
        return {
            "rank": rank,
            "clan_id": pk,
            "name": row.clan.name,
            "tag": row.clan.tag,
            "crest": None,
            "hq_level": row.hq_level,
            "member_count": row.member_count,
            "capacity": effective_capacity(row, config.clan) if config.clan else row.base_capacity,
            "competitive_score": final[pk].final_competitive_score
            if closed
            else row.competitive_score,
        }

    return {
        **_metadata(season, offset, limit, total),
        "items": [item(pk, rank) for pk, rank in ranked],
        "own_standing": item(own_id, own_rank) if own_id and own_rank else None,
    }


@router.get("/state", response=b3_responses(ArenaOutput))
def arena_state(request: HttpRequest) -> dict[str, Any]:
    _player(request)
    season = resolve_current_season(at=timezone.now(), required=False)
    seasons = Season.objects.filter(status__in=["ACTIVE", "CLOSED", "ARCHIVED"]).order_by(
        "-starts_at"
    )[:50]

    def output(row: Season) -> dict[str, Any]:
        return {
            "code": row.code,
            "title": row.title,
            "status": row.status,
            "starts_at": row.starts_at,
            "ends_at": row.ends_at,
        }

    return {
        "season": output(season) if season else None,
        "seasons": [output(row) for row in seasons],
        "warfront": WarfrontOutput().model_dump(),
        "wallet_enabled": False,
        "account_link_enabled": False,
    }


@router.get("/dlx", response=b3_responses(DlxOutput))
@transaction.atomic
def dlx_balance(request: HttpRequest, season: str | None = None) -> dict[str, Any]:
    player = Player.objects.select_for_update().get(pk=_player(request).pk)
    selected = _season(season)
    return {
        "season": selected.code,
        **player_dlx_balance(player, selected),
        "claimable": 0,
        "claimable_enabled": False,
        "transferable": False,
        "tradable": False,
        "on_chain": False,
        "price_status": "UNAVAILABLE",
    }
