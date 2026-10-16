"""Broker publication follows the outermost business commit; scanner remains recovery truth."""

from datetime import datetime

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone


def dispatch_delivery(delivery_id: int, due: datetime | None = None) -> None:
    def publish() -> None:
        try:
            from apps.notifications.tasks import deliver_one_notification

            countdown = max(0, (due - timezone.now()).total_seconds()) if due else 0
            options = (
                {"queue": settings.NOTIFICATION_INTERACTIVE_QUEUE}
                if settings.NOTIFICATION_INTERACTIVE_QUEUE
                else {}
            )
            deliver_one_notification.apply_async(args=[delivery_id], countdown=countdown, **options)
            structlog.get_logger(__name__).info(
                "telegram_delivery_enqueued",
                delivery_id=delivery_id,
                queue=settings.NOTIFICATION_INTERACTIVE_QUEUE or "default",
                countdown=countdown,
            )
        except Exception:
            structlog.get_logger(__name__).warning(
                "notification_dispatch_deferred", delivery_id=delivery_id
            )

    transaction.on_commit(publish)
