import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone
from django_redis.cache import RedisCache  # type: ignore[import-untyped]

from apps.arena.models import RankingProjectionEvent, RankingProjectionState
from apps.clans.models import ClanSeason
from apps.game_config.services import resolve_game_config_version
from apps.seasons.models import PlayerSeason, Season

MAX_ORDER_VALUE = 9_999_999_999_999_999_999


class RankingProjectionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RankingSlice:
    ranked_ids: tuple[tuple[int, int], ...]
    own_rank: int | None


def _redis() -> Any:
    return cast(RedisCache, cache).client.get_client(write=True)


def _member(reached_at: datetime, entity_id: int) -> str:
    micros = int(reached_at.astimezone(UTC).timestamp() * 1_000_000)
    return f"{MAX_ORDER_VALUE - micros:019d}:{MAX_ORDER_VALUE - entity_id:019d}:{entity_id}"


def _entity_id(member: bytes | str) -> int:
    text = member.decode() if isinstance(member, bytes) else member
    return int(text.rsplit(":", 1)[1])


def ranking_key(season_id: int, entity_type: str) -> str:
    return f"arena:v2:season:{season_id}:{entity_type.lower()}"


def _keys(target: str) -> tuple[str, str, str, str]:
    return target, f"{target}:members", f"{target}:generations", f"{target}:ready"


def _publish(
    *,
    season_id: int,
    entity_type: str,
    entity_id: int,
    score: int,
    reached_at: datetime | None,
    generation: int,
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
    event, created = RankingProjectionEvent.objects.get_or_create(
        entity_type=entity_type,
        source_id=source_id,
        defaults={
            "season_id": season_id,
            "entity_id": entity_id,
            "score": score,
            "score_reached_at": reached_at or timezone.now(),
            "operation": operation,
            "generation": generation,
        },
    )
    if not created and (
        event.season_id,
        event.entity_id,
        event.score,
        event.operation,
        event.generation,
    ) != (season_id, entity_id, score, operation, generation):
        raise ValueError("Ranking event idempotency conflict")
    transaction.on_commit(lambda: dispatch_projection_safely(event.pk))
    return event


def publish_player_ranking(row: PlayerSeason, *, source_id: str) -> RankingProjectionEvent:
    return _publish(
        season_id=row.season_id,
        entity_type=RankingProjectionEvent.Entity.PLAYER,
        entity_id=row.player_id,
        score=row.season_points,
        reached_at=row.score_reached_at,
        generation=row.ranking_generation,
        source_id=source_id,
    )


def publish_clan_ranking(row: ClanSeason, *, source_id: str) -> RankingProjectionEvent:
    return _publish(
        season_id=row.season_id,
        entity_type=RankingProjectionEvent.Entity.CLAN,
        entity_id=row.pk,
        score=row.competitive_score,
        reached_at=row.score_reached_at,
        generation=row.ranking_generation,
        source_id=source_id,
    )


def dispatch_projection_safely(event_id: int) -> None:
    try:
        from apps.arena.tasks import consume_ranking_event

        consume_ranking_event.delay(event_id)
    except Exception:
        # The durable event remains pending for Beat. Redis cannot block canonical settlement.
        return


_APPLY_SCRIPT = """
local current = tonumber(redis.call('hget', KEYS[3], ARGV[1]) or '-1')
local incoming = tonumber(ARGV[2])
if current > incoming then return 0 end
local old = redis.call('hget', KEYS[2], ARGV[1])
if old then redis.call('zrem', KEYS[1], old) end
if ARGV[3] == 'UPSERT' then
  redis.call('zadd', KEYS[1], ARGV[4], ARGV[5])
  redis.call('hset', KEYS[2], ARGV[1], ARGV[5])
else
  redis.call('hdel', KEYS[2], ARGV[1])
end
redis.call('hset', KEYS[3], ARGV[1], incoming)
redis.call('set', KEYS[4], '1')
return 1
"""


def apply_projection_state(
    *,
    target: str,
    entity_id: int,
    generation: int,
    operation: str,
    score: int,
    reached_at: datetime,
) -> bool:
    member = _member(reached_at, entity_id)
    return bool(
        _redis().eval(
            _APPLY_SCRIPT, 4, *_keys(target), entity_id, generation, operation, score, member
        )
    )


def apply_projection_event(
    event: RankingProjectionEvent, *, target: str | None = None, acknowledge: bool = True
) -> bool:
    live = ranking_key(event.season_id, event.entity_type)
    destination = target or live
    applied = apply_projection_state(
        target=destination,
        entity_id=event.entity_id,
        generation=event.generation,
        operation=event.operation,
        score=event.score,
        reached_at=event.score_reached_at,
    )
    if acknowledge and destination == live:
        RankingProjectionEvent.objects.filter(pk=event.pk, processed_at__isnull=True).update(
            processed_at=timezone.now()
        )
    return applied


def consume_pending_events(limit: int = 500) -> int:
    rows = list(
        RankingProjectionEvent.objects.filter(processed_at__isnull=True).order_by("id")[:limit]
    )
    for row in rows:
        apply_projection_event(row)
    return len(rows)


def read_ranking_slice(
    *,
    season_id: int,
    entity_type: str,
    offset: int,
    limit: int,
    own_entity_id: int | None,
) -> RankingSlice:
    target = ranking_key(season_id, entity_type)
    client = _redis()
    try:
        if not client.get(f"{target}:ready"):
            dispatch_rebuild_safely(season_id, entity_type)
            raise RankingProjectionUnavailable("Ranking projection is rebuilding")
        raw_members = client.zrevrange(target, offset, offset + limit - 1)
        ranked_ids = tuple(
            (_entity_id(member), offset + index) for index, member in enumerate(raw_members, 1)
        )
        own_rank = None
        if own_entity_id is not None:
            member = client.hget(f"{target}:members", own_entity_id)
            if member is not None:
                zero_rank = client.zrevrank(target, member)
                own_rank = int(zero_rank) + 1 if zero_rank is not None else None
        return RankingSlice(ranked_ids, own_rank)
    except Exception as exc:
        dispatch_rebuild_safely(season_id, entity_type)
        raise RankingProjectionUnavailable("Ranking projection is temporarily unavailable") from exc


def dispatch_rebuild_safely(season_id: int, entity_type: str) -> None:
    try:
        from apps.arena.tasks import rebuild_one_ranking

        rebuild_one_ranking.delay(season_id, entity_type)
    except Exception:
        return


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
    return bool(
        _redis().eval(
            _OWNER_SCRIPT,
            1,
            f"{ranking_key(season_id, entity_type)}:rebuild-lock",
            token,
            lease_seconds,
        )
    )


def release_rebuild_lock(season_id: int, entity_type: str, token: str) -> bool:
    return bool(
        _redis().eval(
            _OWNER_SCRIPT,
            1,
            f"{ranking_key(season_id, entity_type)}:rebuild-lock",
            token,
            "release",
        )
    )


_PROMOTE_SCRIPT = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
for i=2,5 do
  if redis.call('exists', KEYS[i]) == 1 then redis.call('rename', KEYS[i], KEYS[i+4])
  else redis.call('del', KEYS[i+4]) end
end
return 1
"""


def promote_rebuild(*, season_id: int, entity_type: str, token: str, temporary_target: str) -> bool:
    live = ranking_key(season_id, entity_type)
    return bool(
        _redis().eval(
            _PROMOTE_SCRIPT,
            9,
            f"{live}:rebuild-lock",
            *_keys(temporary_target),
            *_keys(live),
            token,
        )
    )


def rebuild_ranking(season: Season, entity_type: str) -> bool:
    token = acquire_rebuild_lock(season.pk, entity_type)
    if token is None:
        return False
    client = _redis()
    live = ranking_key(season.pk, entity_type)
    temporary = f"{live}:tmp:{token}"
    lease = settings.ARENA_REBUILD_LOCK_SECONDS
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
            ).values_list("player_id", "season_points", "score_reached_at", "ranking_generation")
        else:
            rows = ClanSeason.objects.filter(
                season=season, competitive_score__gte=policy.minimum_score
            ).values_list("id", "competitive_score", "score_reached_at", "ranking_generation")
        client.delete(*_keys(temporary))
        for index, (entity_id, score, reached, generation) in enumerate(
            rows.iterator(chunk_size=500), 1
        ):
            if reached is None:
                raise RankingProjectionUnavailable("Positive score lacks tie provenance")
            apply_projection_state(
                target=temporary,
                entity_id=entity_id,
                generation=generation,
                operation=RankingProjectionEvent.Operation.UPSERT,
                score=score,
                reached_at=reached,
            )
            if index % 500 == 0 and not renew_rebuild_lock(season.pk, entity_type, token, lease):
                return False
        if not renew_rebuild_lock(season.pk, entity_type, token, lease):
            return False
        for event in RankingProjectionEvent.objects.filter(
            season=season, entity_type=entity_type, id__gt=watermark
        ).order_by("id"):
            apply_projection_event(event, target=temporary, acknowledge=False)
        client.set(f"{temporary}:ready", "1")
        if not promote_rebuild(
            season_id=season.pk,
            entity_type=entity_type,
            token=token,
            temporary_target=temporary,
        ):
            return False
        # Durable tail replay after promotion closes updates racing the swap. Generation checks
        # make duplicate/out-of-order workers harmless.
        tail = list(
            RankingProjectionEvent.objects.filter(
                season=season, entity_type=entity_type, id__gt=watermark
            ).order_by("id")
        )
        for event in tail:
            apply_projection_event(event)
        if not renew_rebuild_lock(season.pk, entity_type, token, lease):
            return False
        high = tail[-1].pk if tail else watermark
        RankingProjectionState.objects.update_or_create(
            season=season, entity_type=entity_type, defaults={"high_watermark": high}
        )
        return True
    finally:
        client.delete(*_keys(temporary))
        release_rebuild_lock(season.pk, entity_type, token)


def player_ranking_queryset(season: Season) -> QuerySet[PlayerSeason]:
    """Canonical reconciliation ordering; Arena request handlers intentionally do not use it."""
    return PlayerSeason.objects.filter(season=season).order_by(
        "-season_points", F("score_reached_at").asc(nulls_last=True), "player_id"
    )
