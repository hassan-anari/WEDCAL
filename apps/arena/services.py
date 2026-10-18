import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone
from django_redis.cache import RedisCache  # type: ignore[import-untyped]

from apps.arena.models import RankingProjectionEvent, RankingProjectionState
from apps.clans.models import ClanSeason
from apps.game_config.services import resolve_game_config_version
from apps.seasons.models import (
    ClanSeasonFinalStanding,
    PlayerSeason,
    PlayerSeasonFinalStanding,
    Season,
    SeasonCloseRun,
)

MAX_ORDER_VALUE = 9_999_999_999_999_999_999


class RankingProjectionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RankingSlice:
    ranked_ids: tuple[tuple[int, int], ...]
    own_rank: int | None
    scores: dict[int, int] = field(default_factory=dict)
    generations: dict[int, int] = field(default_factory=dict)
    total_count: int = 0


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


@transaction.atomic
def _publish(
    *,
    season_id: int,
    entity_type: str,
    entity_id: int,
    score: int,
    reached_at: datetime | None,
    generation: int,
    source_id: str,
    eligible: bool = True,
) -> RankingProjectionEvent:
    from apps.seasons.barrier import acquire_economic_write_barrier

    acquire_economic_write_barrier(season_id)
    policy = resolve_game_config_version(
        Season.objects.only("baseline_config_id").get(pk=season_id).baseline_config_id
    ).rankings
    operation = (
        RankingProjectionEvent.Operation.UPSERT
        if eligible and score >= policy.minimum_score
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
        eligible=row.state != ClanSeason.State.ENDED,
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
    season = Season.objects.get(pk=event.season_id)
    if (
        season.status in (Season.Status.CLOSED, Season.Status.ARCHIVED)
        or SeasonCloseRun.objects.filter(
            season=season, barrier_established_at__isnull=False
        ).exists()
    ):
        from apps.arena.models import EconomicEpoch, EpochAnomaly

        epoch = EconomicEpoch.objects.filter(season=season).order_by("-epoch_number").first()
        if epoch is not None:
            EpochAnomaly.objects.get_or_create(
                epoch=epoch,
                provider="RANKING_AFTER_FINALIZATION_BARRIER",
                source_id=str(event.pk),
                defaults={
                    "source_time": event.created_at,
                    "details": {"entity": event.entity_type},
                },
            )
        raise RankingProjectionUnavailable("Ranking event arrived after finalization barrier")
    operation, generation = event.operation, event.generation
    if event.entity_type == RankingProjectionEvent.Entity.CLAN:
        clan = ClanSeason.objects.get(pk=event.entity_id, season_id=event.season_id)
        if clan.state == ClanSeason.State.ENDED:
            operation = RankingProjectionEvent.Operation.REMOVE
            generation = max(generation, clan.ranking_generation)
    live = ranking_key(event.season_id, event.entity_type)
    destination = target or live
    applied = apply_projection_state(
        target=destination,
        entity_id=event.entity_id,
        generation=generation,
        operation=operation,
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


_READ_SCRIPT = """
if not redis.call('get', KEYS[4]) then return false end
local members = redis.call('zrevrange', KEYS[1], ARGV[1], ARGV[2], 'WITHSCORES')
local rows = {}
for i=1,#members,2 do
  local entity = string.match(members[i], ':(%d+)$')
  local gen = redis.call('hget', KEYS[3], entity) or '-1'
  table.insert(rows, {members[i], members[i+1], gen})
end
local own = redis.call('hget', KEYS[2], ARGV[3]) or ''
local score = own ~= '' and redis.call('zscore', KEYS[1], own) or ''
local gen = redis.call('hget', KEYS[3], ARGV[3]) or '-1'
local rank = own ~= '' and redis.call('zrevrank', KEYS[1], own) or -1
return {rows, own, score or '', gen, rank, redis.call('zcard', KEYS[1])}
"""


def read_ranking_slice(
    *,
    season_id: int,
    entity_type: str,
    offset: int,
    limit: int,
    own_entity_id: int | None,
) -> RankingSlice:
    target = ranking_key(season_id, entity_type)
    try:
        client = _redis()
        result = client.eval(
            _READ_SCRIPT, 4, *_keys(target), offset, offset + limit - 1, own_entity_id or ""
        )
        if not result:
            raise RankingProjectionUnavailable("Ranking projection is rebuilding")
        members, own_member, own_score, own_generation, own_rank_value, total = result
        ranked_ids = tuple(
            (_entity_id(row[0]), offset + index) for index, row in enumerate(members, 1)
        )
        scores = {_entity_id(row[0]): int(float(row[1])) for row in members}
        generations = {_entity_id(row[0]): int(row[2]) for row in members}
        own_rank = int(own_rank_value) + 1 if int(own_rank_value) >= 0 else None
        if own_member:
            scores[_entity_id(own_member)] = int(float(own_score))
            generations[_entity_id(own_member)] = int(own_generation)
        if entity_type == "CLAN":
            season = Season.objects.get(pk=season_id)
            if season.status not in {Season.Status.CLOSED, Season.Status.ARCHIVED} and int(
                total
            ) != live_clan_population(season):
                raise RankingProjectionUnavailable("Clan lifecycle projection is reconciling")
        return RankingSlice(ranked_ids, own_rank, scores, generations, int(total))
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
        closed = season.status in (Season.Status.CLOSED, Season.Status.ARCHIVED)
        watermark = (
            RankingProjectionEvent.objects.filter(season=season, entity_type=entity_type)
            .order_by("-id")
            .values_list("id", flat=True)
            .first()
            or 0
        )
        policy = resolve_game_config_version(season.baseline_config_id).rankings
        rows: Any
        if (
            season.status in (Season.Status.CLOSED, Season.Status.ARCHIVED)
            and entity_type == RankingProjectionEvent.Entity.PLAYER
        ):
            rows = PlayerSeasonFinalStanding.objects.filter(season=season).values_list(
                "player_id", "final_season_points", "score_reached_at", "ranking_generation"
            )
        elif season.status in (Season.Status.CLOSED, Season.Status.ARCHIVED):
            rows = ClanSeasonFinalStanding.objects.filter(season=season).values_list(
                "clan_season_id",
                "final_competitive_score",
                "score_reached_at",
                "ranking_generation",
            )
        elif entity_type == RankingProjectionEvent.Entity.PLAYER:
            rows = PlayerSeason.objects.filter(
                season=season, season_points__gte=policy.minimum_score
            ).values_list("player_id", "season_points", "score_reached_at", "ranking_generation")
        else:
            rows = ClanSeason.objects.filter(season=season).values_list(
                "id", "competitive_score", "score_reached_at", "ranking_generation", "state"
            )
        client.delete(*_keys(temporary))
        for index, values in enumerate(rows.iterator(chunk_size=500), 1):
            entity_id, score, reached, generation = values[:4]
            operation = RankingProjectionEvent.Operation.UPSERT
            if (
                not closed
                and entity_type == "CLAN"
                and (values[4] == ClanSeason.State.ENDED or score < policy.minimum_score)
            ):
                operation = RankingProjectionEvent.Operation.REMOVE
            if reached is None:
                if operation == RankingProjectionEvent.Operation.UPSERT:
                    raise RankingProjectionUnavailable("Positive score lacks tie provenance")
                reached = timezone.now()
            apply_projection_state(
                target=temporary,
                entity_id=entity_id,
                generation=generation,
                operation=operation,
                score=score,
                reached_at=reached,
            )
            if index % 500 == 0 and not renew_rebuild_lock(season.pk, entity_type, token, lease):
                return False
        if not renew_rebuild_lock(season.pk, entity_type, token, lease):
            return False
        if not closed:
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
        tail = (
            []
            if closed
            else list(
                RankingProjectionEvent.objects.filter(
                    season=season, entity_type=entity_type, id__gt=watermark
                ).order_by("id")
            )
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


RankStatus = Literal["RANKED", "UNRANKED", "UNAVAILABLE", "RECONCILING"]


@dataclass(frozen=True, slots=True)
class RankingStanding:
    rank: int | None
    status: RankStatus
    score: int


_BATCH_RANK_SCRIPT = """
if not redis.call('get', KEYS[4]) then return false end
local rows = {}
for i=1,#ARGV do
  local member = redis.call('hget', KEYS[2], ARGV[i])
  local rank = member and redis.call('zrevrank', KEYS[1], member) or false
  local score = member and redis.call('zscore', KEYS[1], member) or ''
  local gen = redis.call('hget', KEYS[3], ARGV[i]) or '-1'
  table.insert(rows, {rank or -1, score or '', gen})
end
return {rows, redis.call('zcard', KEYS[1])}
"""


def read_player_standings(season: Season, player_ids: list[int]) -> dict[int, RankingStanding]:
    ids = list(dict.fromkeys(player_ids))
    rows = (
        {r.player_id: r for r in PlayerSeason.objects.filter(season=season, player_id__in=ids)}
        if ids
        else {}
    )
    return _read_standings(season, "PLAYER", ids, rows, "season_points")


def live_clan_population(season: Season) -> int:
    minimum = resolve_game_config_version(season.baseline_config_id).rankings.minimum_score
    return (
        ClanSeason.objects.filter(season=season, competitive_score__gte=minimum)
        .exclude(state=ClanSeason.State.ENDED)
        .count()
    )


def read_clan_standings(season: Season, clan_season_ids: list[int]) -> dict[int, RankingStanding]:
    ids = list(dict.fromkeys(clan_season_ids))
    if len(ids) > 100:
        raise ValueError("Ranking summary is limited to 100 entities")
    if season.status in {Season.Status.CLOSED, Season.Status.ARCHIVED}:
        final = {
            r.clan_season_id: r
            for r in ClanSeasonFinalStanding.objects.filter(season=season, clan_season_id__in=ids)
        }
        return {
            pk: RankingStanding(final[pk].final_rank, "RANKED", final[pk].final_competitive_score)
            if pk in final
            else RankingStanding(None, "UNRANKED", 0)
            for pk in ids
        }
    rows = {r.pk: r for r in ClanSeason.objects.filter(season=season, pk__in=ids)} if ids else {}
    return _read_standings(season, "CLAN", ids, rows, "competitive_score")


def _read_standings(
    season: Season, entity: str, ids: list[int], rows: dict[int, Any], score_field: str
) -> dict[int, RankingStanding]:
    """Canonical Arena summary: one atomic Redis read; no independent leaderboard."""
    if len(ids) > 100:
        raise ValueError("Ranking summary is limited to 100 entities")
    if not ids:
        return {}
    points = {pk: int(getattr(rows[pk], score_field)) if pk in rows else 0 for pk in ids}

    def unavailable(status: RankStatus) -> dict[int, RankingStanding]:
        return {pk: RankingStanding(None, status, points[pk]) for pk in ids}

    pending = RankingProjectionEvent.objects.filter(
        season=season, entity_type=entity, processed_at__isnull=True
    )
    if pending.exists():
        return unavailable("RECONCILING")
    minimum = resolve_game_config_version(season.baseline_config_id).rankings.minimum_score
    try:
        result = _redis().eval(_BATCH_RANK_SCRIPT, 4, *_keys(ranking_key(season.pk, entity)), *ids)
        if not result:
            raise RankingProjectionUnavailable
        result, total = result
        if len(result) != len(ids):
            raise RankingProjectionUnavailable
        if entity == "CLAN" and int(total) != live_clan_population(season):
            dispatch_rebuild_safely(season.pk, entity)
            return unavailable("RECONCILING")
        standings = {}
        for pk, (rank, score, generation) in zip(ids, result, strict=True):
            row = rows.get(pk)
            ranked = int(rank) >= 0
            if (
                ranked
                != (
                    row is not None
                    and points[pk] >= minimum
                    and (entity != "CLAN" or row.state != ClanSeason.State.ENDED)
                )
                or ranked
                and (
                    row is None
                    or int(float(score)) != points[pk]
                    or int(generation) != row.ranking_generation
                )
            ):
                dispatch_rebuild_safely(season.pk, entity)
                return unavailable("RECONCILING")
            standings[pk] = RankingStanding(
                int(rank) + 1 if ranked else None, "RANKED" if ranked else "UNRANKED", points[pk]
            )
    except Exception:
        dispatch_rebuild_safely(season.pk, entity)
        return unavailable("UNAVAILABLE")
    if pending.exists():
        return unavailable("RECONCILING")
    return standings
