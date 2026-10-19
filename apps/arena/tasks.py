import structlog
from celery import shared_task
from django.conf import settings
from django.db import OperationalError

from apps.arena.models import RankingProjectionEvent
from apps.arena.services import apply_projection_event, consume_pending_events, rebuild_ranking
from apps.seasons.models import Season

logger = structlog.get_logger(__name__)


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
    # Compatibility entry point. All scheduled mutations require per-Season authority.
    return int(settle_epochs())


@shared_task(name="apps.arena.tasks.evaluate_growth_unlocks")  # type: ignore[untyped-decorator]
def evaluate_growth_unlocks_task() -> int:
    # Growth runs under the same activation/pause and worker lock as weekly settlement.
    return int(settle_epochs())


@shared_task(  # type: ignore[untyped-decorator]
    name="apps.arena.tasks.run_one_season_economics",
    autoretry_for=(OperationalError, ConnectionError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=settings.DLX_AUTOMATION_SOFT_TIME_LIMIT,
    time_limit=settings.DLX_AUTOMATION_TIME_LIMIT,
)
def run_one_season_economics(season_id: int) -> int:
    from apps.arena.automation import run_weekly_lifecycle

    return run_weekly_lifecycle(Season.objects.get(pk=season_id))


@shared_task(name="apps.arena.tasks.settle_epochs")  # type: ignore[untyped-decorator]
def settle_epochs() -> int:
    from django.db.models import OuterRef, Subquery

    from apps.arena.models import EconomicAutomationControl

    latest = EconomicAutomationControl.objects.filter(season_id=OuterRef("pk")).order_by("-pk")
    seasons = (
        Season.objects.filter(status="ACTIVE")
        .annotate(automation_enabled=Subquery(latest.values("enabled")[:1]))
        .filter(automation_enabled=True)
    )
    count = 0
    for season_id in seasons.values_list("pk", flat=True):
        run_one_season_economics.delay(season_id)
        count += 1
    return count


@shared_task(name="apps.arena.tasks.monitor_economic_operations")  # type: ignore[untyped-decorator]
def monitor_economic_operations() -> int:
    from apps.arena.alert_delivery import (
        CHANNEL,
        recover_expired_deliveries,
        retry_pending_fanouts,
        route_health,
    )
    from apps.arena.alerts import scan_alerts
    from apps.arena.models import EconomicAlertDelivery

    if settings.ECONOMIC_ALERTS_ENABLED or settings.ECONOMIC_ALERT_DELIVERY_ENABLED:
        route_health(report_errors=True)
    detected = scan_alerts()
    if settings.ECONOMIC_ALERTS_ENABLED or settings.ECONOMIC_ALERT_DELIVERY_ENABLED:
        retry_pending_fanouts()
    if settings.ECONOMIC_ALERT_DELIVERY_ENABLED:
        from django.utils import timezone

        recover_expired_deliveries()
        for pk in (
            EconomicAlertDelivery.objects.filter(
                channel=CHANNEL,
                status__in=["PENDING", "UNCERTAIN"],
                next_attempt_at__lte=timezone.now(),
            )
            .order_by("pk")
            .values_list("pk", flat=True)[: settings.ECONOMIC_ALERT_BATCH_SIZE]
        ):
            deliver_economic_alert.delay(pk)
    return detected


@shared_task(  # type: ignore[untyped-decorator]
    name="apps.arena.tasks.deliver_economic_alert", acks_late=True, reject_on_worker_lost=True
)
def deliver_economic_alert(delivery_id: int) -> bool:
    from apps.arena.alert_delivery import deliver_alert

    return deliver_alert(delivery_id)
