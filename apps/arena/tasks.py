from celery import shared_task
from django.utils import timezone

from apps.arena.epochs import (
    EpochBlocked,
    ScoringPolicyUnconfigured,
    evaluate_growth_unlocks,
    freeze_epoch,
)
from apps.arena.models import EconomicEpoch, RankingProjectionEvent
from apps.arena.services import apply_projection_event, consume_pending_events, rebuild_ranking
from apps.seasons.models import Season


@shared_task(  # type: ignore[untyped-decorator]
    name="apps.arena.tasks.consume_ranking_event",
    autoretry_for=(ConnectionError,),
    retry_backoff=True,
    max_retries=5,
)
def consume_ranking_event(event_id: int) -> None:
    apply_projection_event(RankingProjectionEvent.objects.get(pk=event_id))


@shared_task(name="apps.arena.tasks.consume_ranking_projections")  # type: ignore[untyped-decorator]
def consume_ranking_projections() -> int:
    return consume_pending_events()


@shared_task(name="apps.arena.tasks.reconcile_rankings")  # type: ignore[untyped-decorator]
def reconcile_rankings() -> int:
    count = 0
    for season in Season.objects.filter(status=Season.Status.ACTIVE):
        for kind in RankingProjectionEvent.Entity.values:
            count += int(rebuild_ranking(season, kind))
    return count


@shared_task(name="apps.arena.tasks.rebuild_one_ranking")  # type: ignore[untyped-decorator]
def rebuild_one_ranking(season_id: int, entity_type: str) -> bool:
    return rebuild_ranking(Season.objects.get(pk=season_id), entity_type)


@shared_task(name="apps.arena.tasks.drain_and_freeze_epochs")  # type: ignore[untyped-decorator]
def drain_and_freeze_epochs() -> int:
    count = 0
    for epoch in EconomicEpoch.objects.filter(
        state__in=["OPEN", "DRAINING"], ends_at__lte=timezone.now()
    ).order_by("season_id", "epoch_number"):
        try:
            freeze_epoch(epoch)
            count += 1
        except EpochBlocked:
            continue
    return count


@shared_task(name="apps.arena.tasks.evaluate_growth_unlocks")  # type: ignore[untyped-decorator]
def evaluate_growth_unlocks_task() -> int:
    total = 0
    for season in Season.objects.filter(status=Season.Status.ACTIVE):
        try:
            total += len(evaluate_growth_unlocks(season))
        except ScoringPolicyUnconfigured:
            continue
    return total


@shared_task(name="apps.arena.tasks.settle_epochs")  # type: ignore[untyped-decorator]
def settle_epochs() -> int:
    # Canonical config intentionally has no scoring strategies; fail closed without allocations.
    return 0
