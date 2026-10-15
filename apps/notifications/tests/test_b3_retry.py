# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.notifications.models import DeliveryState, Notification, TelegramDelivery
from apps.notifications.services import _deliver
from apps.notifications.tests.test_services import player
from apps.telegram.bot_api import TelegramResult

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("category", ["local_rate_limited", "rate_limited", "budget_unavailable"])
def test_temporary_admission_never_permanently_fails_even_at_attempt_limit(
    monkeypatch, settings, category
):
    p = player()
    notification = Notification.objects.create(
        recipient=p,
        event_key="TEST",
        source_domain="TEST",
        source_type="TEST",
        source_id="1",
        idempotency_key="retry",
    )
    import uuid

    row = TelegramDelivery.objects.create(
        notification=notification,
        telegram_user_id=p.telegram_user_id,
        state=DeliveryState.PROCESSING,
        lease_token=uuid.uuid4(),
        attempt_count=settings.NOTIFICATION_MAX_ATTEMPTS,
    )
    monkeypatch.setattr(
        "apps.notifications.services.bot_api.send_message",
        lambda *_: TelegramResult(False, category=category, retry_after=17),
    )
    before = timezone.now()
    _deliver(row, "synthetic")
    row.refresh_from_db()
    assert row.state == DeliveryState.RETRY_WAIT
    assert row.next_attempt_at >= before + timedelta(seconds=17)
