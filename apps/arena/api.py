from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema
from ninja.errors import HttpError

from apps.arena.epochs import player_dlx_balance
from apps.arena.services import clan_ranking_queryset, player_ranking_queryset, standing
from apps.clans.economy import effective_capacity
from apps.clans.models import ClanMembership
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
    qs = player_ranking_queryset(season).filter(season_points__gte=policy.minimum_score)
    page = list(qs[offset : offset + limit])
    own_row = PlayerSeason.objects.filter(player=player, season=season).first()
    own = standing(qs, own_row.pk) if own_row else None

    def item(rank: int, row: PlayerSeason) -> dict[str, Any]:
        membership = (
            ClanMembership.objects.filter(
                player_id=row.player_id, season=season, ended_at__isnull=True
            )
            .select_related("clan_season__clan")
            .first()
        )
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
        "items": [item(offset + i, row) for i, row in enumerate(page, 1)],
        "own_standing": item(own[0], own[1]) if own else None,
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
    qs = clan_ranking_queryset(season).filter(competitive_score__gte=config.rankings.minimum_score)
    page = list(qs[offset : offset + limit])
    membership = ClanMembership.objects.filter(
        player=player, season=season, ended_at__isnull=True
    ).first()
    own = standing(qs, membership.clan_season_id) if membership else None

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
        "items": [item(offset + i, row) for i, row in enumerate(page, 1)],
        "own_standing": item(own[0], own[1]) if own else None,
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
