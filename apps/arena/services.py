import uuid
from datetime import UTC, datetime
from typing import Any, cast

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Q, QuerySet
from django.utils import timezone
from django_redis.cache import RedisCache  # type: ignore[import-untyped]

from apps.arena.models import RankingProjectionEvent, RankingProjectionState
from apps.clans.models import ClanSeason
from apps.game_config.services import resolve_game_config_version
from apps.seasons.models import PlayerSeason, Season

MAX_ORDER_VALUE = 9_999_999_999_999_999_999


def _redis() -> Any:
    return cast(RedisCache, cache).client.get_client(write=True)


def _member(reached_at: datetime, entity_id: int) -> str:
    micros = int(reached_at.astimezone(UTC).timestamp() * 1_000_000)
    return f"{MAX_ORDER_VALUE - micros:019d}:{MAX_ORDER_VALUE - entity_id:019d}:{entity_id}"


def ranking_key(season_id: int, entity_type: str) -> str:
    return f"arena:v1:season:{season_id}:{entity_type.lower()}"


def _publish(
    *,
    season_id: int,
    entity_type: str,
    entity_id: int,
    score: int,
    reached_at: datetime | None,
    source_id: str,
) -> RankingProjectionEvent:
    policy = resolve_game_config_version(
        Season.objects.only("baseline_config_id").get(pk=season_id).baseline_config_id
    ).rankings
    operation = (
        RankingProjectionEvent.Operation.UPSERT
        if score >= policy.minimum_score
        else RankingProjectionEvent.Operation.REMOVE
    )
    event, _ = RankingProjectionEvent.objects.get_or_create(
        entity_type=entity_type,
        source_id=source_id,
        defaults={
            "season_id": season_id,
            "entity_id": entity_id,
            "score": score,
            "score_reached_at": reached_at or timezone.now(),
            "operation": operation,
        },
    )
    transaction.on_commit(lambda: dispatch_projection_safely(event.pk))
    return event


def publish_player_ranking(row: PlayerSeason, *, source_id: str) -> RankingProjectionEvent:
    return _publish(
        season_id=row.season_id,
        entity_type=RankingProjectionEvent.Entity.PLAYER,
        entity_id=row.player_id,
        score=row.season_points,
        reached_at=row.score_reached_at,
        source_id=source_id,
    )


def publish_clan_ranking(row: ClanSeason, *, source_id: str) -> RankingProjectionEvent:
    return _publish(
        season_id=row.season_id,
        entity_type=RankingProjectionEvent.Entity.CLAN,
        entity_id=row.pk,
        score=row.competitive_score,
        reached_at=row.score_reached_at,
        source_id=source_id,
    )


def dispatch_projection_safely(event_id: int) -> None:
    try:
        from apps.arena.tasks import consume_ranking_event

        consume_ranking_event.delay(event_id)
    except Exception:
        pass  # durable event is consumed by Beat; canonical settlement must not depend on Redis


def apply_projection_event(event: RankingProjectionEvent, *, key: str | None = None) -> None:
    client = _redis()
    target = key or ranking_key(event.season_id, event.entity_type)
    mapping = f"{target}:members"
    entity = str(event.entity_id)
    old = client.hget(mapping, entity)
    pipe = client.pipeline(transaction=True)
    if old:
        pipe.zrem(target, old)
    if event.operation == RankingProjectionEvent.Operation.UPSERT:
        member = _member(event.score_reached_at, event.entity_id)
        pipe.zadd(target, {member: event.score})
        pipe.hset(mapping, entity, member)
    else:
        pipe.hdel(mapping, entity)
    pipe.execute()
    RankingProjectionEvent.objects.filter(pk=event.pk, processed_at__isnull=True).update(
        processed_at=timezone.now()
    )


def consume_pending_events(limit: int = 500) -> int:
    rows = list(
        RankingProjectionEvent.objects.filter(processed_at__isnull=True).order_by("id")[:limit]
    )
    for row in rows:
        apply_projection_event(row)
    return len(rows)


def acquire_rebuild_lock(
    season_id: int, entity_type: str, lease_seconds: int | None = None
) -> str | None:
    token = uuid.uuid4().hex
    key = f"{ranking_key(season_id, entity_type)}:rebuild-lock"
    ok = _redis().set(key, token, nx=True, ex=lease_seconds or settings.ARENA_REBUILD_LOCK_SECONDS)
    return token if ok else None


_OWNER_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  if ARGV[2] == 'release' then return redis.call('del', KEYS[1]) end
  return redis.call('expire', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


def renew_rebuild_lock(season_id: int, entity_type: str, token: str, lease_seconds: int) -> bool:
    client = _redis()
    return bool(
        client.eval(
            _OWNER_SCRIPT,
            1,
            f"{ranking_key(season_id, entity_type)}:rebuild-lock",
            token,
            lease_seconds,
        )
    )


def release_rebuild_lock(season_id: int, entity_type: str, token: str) -> bool:
    client = _redis()
    return bool(
        client.eval(
            _OWNER_SCRIPT,
            1,
            f"{ranking_key(season_id, entity_type)}:rebuild-lock",
            token,
            "release",
        )
    )


def rebuild_ranking(season: Season, entity_type: str) -> bool:
    token = acquire_rebuild_lock(season.pk, entity_type)
    if token is None:
        return False
    client = _redis()
    live = ranking_key(season.pk, entity_type)
    temp = f"{live}:tmp:{token}"
    try:
        watermark = (
            RankingProjectionEvent.objects.filter(season=season, entity_type=entity_type)
            .order_by("-id")
            .values_list("id", flat=True)
            .first()
            or 0
        )
        policy = resolve_game_config_version(season.baseline_config_id).rankings
        if entity_type == RankingProjectionEvent.Entity.PLAYER:
            rows: Any = PlayerSeason.objects.filter(
                season=season, season_points__gte=policy.minimum_score
            ).values_list("player_id", "season_points", "score_reached_at")
        else:
            rows = ClanSeason.objects.filter(
                season=season, competitive_score__gte=policy.minimum_score
            ).values_list("id", "competitive_score", "score_reached_at")
        pipe = client.pipeline(transaction=False)
        pipe.delete(temp, f"{temp}:members")
        for entity_id, score, reached in rows.iterator(chunk_size=1000):
            member = _member(reached or season.starts_at, entity_id)
            pipe.zadd(temp, {member: score})
            pipe.hset(f"{temp}:members", str(entity_id), member)
        pipe.execute()
        for event in RankingProjectionEvent.objects.filter(
            season=season, entity_type=entity_type, id__gt=watermark
        ).order_by("id"):
            apply_projection_event(event, key=temp)
        if not renew_rebuild_lock(
            season.pk, entity_type, token, settings.ARENA_REBUILD_LOCK_SECONDS
        ):
            return False
        # Promotion is atomic; later events remain durable and are consumed normally.
        client.eval(
            "if redis.call('exists',KEYS[1]) == 1 then redis.call('rename',KEYS[1],KEYS[2]) "
            "else redis.call('del',KEYS[2]) end; "
            "if redis.call('exists',KEYS[3]) == 1 then redis.call('rename',KEYS[3],KEYS[4]) "
            "else redis.call('del',KEYS[4]) end; return 1",
            4,
            temp,
            live,
            f"{temp}:members",
            f"{live}:members",
        )
        for event in RankingProjectionEvent.objects.filter(
            season=season, entity_type=entity_type, id__gt=watermark
        ).order_by("id"):
            apply_projection_event(event)
        RankingProjectionState.objects.update_or_create(
            season=season, entity_type=entity_type, defaults={"high_watermark": watermark}
        )
        return True
    finally:
        client.delete(temp, f"{temp}:members")
        release_rebuild_lock(season.pk, entity_type, token)


def player_ranking_queryset(season: Season) -> QuerySet[PlayerSeason]:
    return (
        PlayerSeason.objects.filter(season=season)
        .select_related("player", "player__progression")
        .order_by("-season_points", F("score_reached_at").asc(nulls_last=True), "player_id")
    )


def clan_ranking_queryset(season: Season) -> QuerySet[ClanSeason]:
    return (
        ClanSeason.objects.filter(season=season)
        .select_related("clan")
        .annotate(member_count=Count("memberships", filter=Q(memberships__ended_at__isnull=True)))
        .order_by("-competitive_score", F("score_reached_at").asc(nulls_last=True), "id")
    )


def standing(queryset: QuerySet[Any], pk: int) -> tuple[int, Any] | None:
    for rank, row in enumerate(queryset.iterator(), 1):
        if row.pk == pk:
            return rank, row
    return None
