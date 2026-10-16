# mypy: disable-error-code="untyped-decorator"
from celery import shared_task

from apps.notifications import services
from apps.notifications.models import BroadcastCampaign


@shared_task(name="apps.notifications.tasks.deliver_notifications")
def deliver_notifications() -> int:
    return services.deliver_notifications()


@shared_task(name="apps.notifications.tasks.materialize_broadcasts")
def materialize_broadcasts() -> int:
    total = 0
    for campaign_id in BroadcastCampaign.objects.filter(
        state=BroadcastCampaign.State.MATERIALIZING
    ).values_list("id", flat=True)[:20]:
        total += services.materialize_campaign(campaign_id)
    for campaign_id in BroadcastCampaign.objects.filter(
        state__in=(
            BroadcastCampaign.State.QUEUED,
            BroadcastCampaign.State.SENDING,
            BroadcastCampaign.State.CANCELLED,
        )
    ).values_list("id", flat=True)[:100]:
        services.reconcile_campaign(campaign_id)
    return total


@shared_task(name="apps.notifications.tasks.deliver_broadcasts")
def deliver_broadcasts() -> int:
    return services.deliver_broadcasts()


@shared_task(
    name="apps.notifications.tasks.deliver_one_notification",
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(),
    max_retries=0,
)
def deliver_one_notification(delivery_id: int) -> int:
    return services.deliver_one_notification(delivery_id)
