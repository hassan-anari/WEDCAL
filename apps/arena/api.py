from typing import Any

from django.db import models
from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema
from ninja.errors import HttpError

from apps.arena.epochs import player_dlx_balance
from apps.arena.models import RankingProjectionEvent
from apps.arena.services import RankingProjectionUnavailable, read_ranking_slice
from apps.clans.economy import effective_capacity
from apps.clans.models import ClanMembership, ClanSeason
from apps.game_config.services import resolve_game_config_version
from apps.players.models import Player
from apps.seasons.models import PlayerSeason
from apps.seasons.services import resolve_current_season
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["arena"], auth=mini_app_auth)


class PageQuery(Schema):
    offset: int = 0
    limit: int | None = None


class RankingResponse(Schema):
    items: list[dict[str, Any]]
    own_standing: dict[str, Any] | None
    offset: int
    limit: int


def _player(request: HttpRequest) -> Player:
    auth = getattr(request, "auth", None)
    if not isinstance(auth, Player):
        raise HttpError(401, "Authentication required")
    return auth


def _page(query: PageQuery, policy: Any) -> tuple[int, int]:
    limit = policy.default_page_size if query.limit is None else query.limit
    if query.offset < 0 or limit < 1 or limit > policy.max_page_size:
        raise HttpError(422, "Invalid ranking pagination")
    return query.offset, limit


@router.get("/rankings/players", response=RankingResponse)
def player_rankings(request: HttpRequest, query: Query[PageQuery]) -> dict[str, Any]:
    player = _player(request)
    season = resolve_current_season(at=timezone.now())
    assert season
    policy = resolve_game_config_version(season.baseline_config_id).rankings
    offset, limit = _page(query, policy)
    own_row = PlayerSeason.objects.filter(player=player, season=season).first()
    try:
        ranking = read_ranking_slice(
            season_id=season.pk,
            entity_type=RankingProjectionEvent.Entity.PLAYER,
            offset=offset,
            limit=limit,
            own_entity_id=player.pk if own_row else None,
        )
    except RankingProjectionUnavailable as exc:
        raise HttpError(503, str(exc)) from exc
    ids = [entity_id for entity_id, _ in ranking.ranked_ids]
    if player.pk not in ids and ranking.own_rank is not None:
        ids.append(player.pk)
    rows = {
        row.player_id: row
        for row in PlayerSeason.objects.filter(season=season, player_id__in=ids).select_related(
            "player", "player__progression"
        )
    }
    memberships = {
        membership.player_id: membership
        for membership in ClanMembership.objects.filter(
            player_id__in=ids, season=season, ended_at__isnull=True
        ).select_related("clan_season__clan")
    }
    if any(entity_id not in rows for entity_id, _ in ranking.ranked_ids):
        raise HttpError(503, "Ranking projection is awaiting reconciliation")

    def item(rank: int, row: PlayerSeason) -> dict[str, Any]:
        membership = memberships.get(row.player_id)
        return {
            "rank": rank,
            "player_id": row.player_id,
            "first_name": row.player.first_name,
            "username": row.player.username,
            "avatar_url": row.player.photo_url,
            "level": row.player.progression.level if hasattr(row.player, "progression") else 1,
            "season_points": row.season_points,
            "clan": (
                {"name": membership.clan_season.clan.name, "tag": membership.clan_season.clan.tag}
                if membership
                else None
            ),
        }

    return {
        "items": [item(rank, rows[entity_id]) for entity_id, rank in ranking.ranked_ids],
        "own_standing": (
            item(ranking.own_rank, rows[player.pk])
            if ranking.own_rank is not None and player.pk in rows
            else None
        ),
        "offset": offset,
        "limit": limit,
    }


@router.get("/rankings/clans", response=RankingResponse)
def clan_rankings(request: HttpRequest, query: Query[PageQuery]) -> dict[str, Any]:
    player = _player(request)
    season = resolve_current_season(at=timezone.now())
    assert season
    config = resolve_game_config_version(season.baseline_config_id)
    offset, limit = _page(query, config.rankings)
    membership = ClanMembership.objects.filter(
        player=player, season=season, ended_at__isnull=True
    ).first()
    try:
        ranking = read_ranking_slice(
            season_id=season.pk,
            entity_type=RankingProjectionEvent.Entity.CLAN,
            offset=offset,
            limit=limit,
            own_entity_id=membership.clan_season_id if membership else None,
        )
    except RankingProjectionUnavailable as exc:
        raise HttpError(503, str(exc)) from exc
    ids = [entity_id for entity_id, _ in ranking.ranked_ids]
    if membership and membership.clan_season_id not in ids and ranking.own_rank is not None:
        ids.append(membership.clan_season_id)
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
    if any(entity_id not in rows for entity_id, _ in ranking.ranked_ids):
        raise HttpError(503, "Ranking projection is awaiting reconciliation")

    def item(rank: int, row: Any) -> dict[str, Any]:
        return {
            "rank": rank,
            "clan_id": row.pk,
            "name": row.clan.name,
            "tag": row.clan.tag,
            "crest": None,
            "hq_level": row.hq_level,
            "member_count": row.member_count,
            "capacity": effective_capacity(row, config.clan) if config.clan else row.base_capacity,
            "competitive_score": row.competitive_score,
        }

    return {
        "items": [item(rank, rows[entity_id]) for entity_id, rank in ranking.ranked_ids],
        "own_standing": (
            item(ranking.own_rank, rows[membership.clan_season_id])
            if membership and ranking.own_rank is not None and membership.clan_season_id in rows
            else None
        ),
        "offset": offset,
        "limit": limit,
    }


@router.get("/state", response=dict[str, Any])
def arena_state(request: HttpRequest) -> dict[str, Any]:
    _player(request)
    return {
        "warfront": {
            "state": "LOCKED",
            "available_season": 2,
            "message": "Warfront arrives in Season 2",
        }
    }


@router.get("/dlx", response=dict[str, Any])
def dlx_balance(request: HttpRequest) -> dict[str, Any]:
    player = _player(request)
    season = resolve_current_season(at=timezone.now())
    assert season
    values = player_dlx_balance(player, season)
    return {
        "season": season.code,
        **values,
        "claimable": 0,
        "claimable_enabled": False,
        "transferable": False,
        "tradable": False,
        "on_chain": False,
        "price_status": "UNAVAILABLE",
    }
