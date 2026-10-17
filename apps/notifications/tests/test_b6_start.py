# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.notifications.models import DeliveryState, TelegramDelivery, WelcomeTemplate
from apps.notifications.services import deliver_one_notification
from apps.notifications.welcome_config import activate_welcome
from apps.telegram.bot_api import TelegramResult
from apps.telegram.services import process_inbound_update
from apps.telegram.tests.test_processing import inbound

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def welcome_world(settings, monkeypatch):
    settings.B3_BOT_WELCOME_ENABLED = settings.B4_PUBLIC_WELCOME_ENABLED = True
    monkeypatch.setattr(
        "apps.notifications.tasks.deliver_one_notification.apply_async", lambda **_: None
    )
    actor = User.objects.create_superuser("b6-welcome")
    templates = [
        WelcomeTemplate.objects.create(
            version=v,
            body=f"Welcome {v}",
            keyboard={
                "inline_keyboard": [
                    [{"text": "Play", "web_app": {"url": "https://dalexio-game.vercel.app/"}}]
                ]
            },
            created_by=actor,
        )
        for v in (1, 2)
    ]
    activate_welcome(templates[0], actor, reason="Synthetic activation")

    def start(update_id):
        return inbound(
            update_id,
            {
                "message": {
                    "message_id": update_id,
                    "date": int(timezone.now().timestamp()),
                    "chat": {"id": 7777, "type": "private"},
                    "from": {"id": 7777, "first_name": "Synthetic"},
                    "text": "/start",
                }
            },
        )

    return actor, templates, start


def test_distinct_starts_duplicate_redelivery_and_template_activation(welcome_world):
    actor, templates, start = welcome_world
    first = start(100)
    process_inbound_update(first.pk)
    process_inbound_update(first.pk)
    assert TelegramDelivery.objects.count() == 1
    activate_welcome(templates[1], actor, reason="New current template")
    assert TelegramDelivery.objects.count() == 1
    process_inbound_update(first.pk)
    second = start(101)
    process_inbound_update(second.pk)
    rows = list(TelegramDelivery.objects.select_related("notification").order_by("pk"))
    assert [r.text_snapshot for r in rows] == [t.body for t in templates]
    assert [r.notification.payload["template_version"] for r in rows] == [1, 2]
    assert [r.notification.payload["telegram_update_id"] for r in rows] == [100, 101]
    assert rows[0].source_received_at == first.received_at
    assert rows[1].message_options["reply_markup"] == templates[1].keyboard


@pytest.mark.parametrize("ambiguous", [False, True])
def test_delivery_failures_do_not_suppress_new_commands_or_replay_updates(
    welcome_world, monkeypatch, ambiguous
):
    _, _, start = welcome_world
    first = start(200)
    process_inbound_update(first.pk)
    delivery = TelegramDelivery.objects.get()
    monkeypatch.setattr(
        "apps.notifications.services.bot_api.request",
        lambda *_: TelegramResult(
            False,
            category="timeout" if ambiguous else "rate_limited",
            retry_after=1,
            ambiguous=ambiguous,
        ),
    )
    assert deliver_one_notification(delivery.pk) == 1
    delivery.refresh_from_db()
    assert delivery.state == (DeliveryState.UNCERTAIN if ambiguous else DeliveryState.RETRY_WAIT)
    process_inbound_update(first.pk)
    second = start(201)
    process_inbound_update(second.pk)
    assert TelegramDelivery.objects.count() == 2
    monkeypatch.setattr(
        "apps.notifications.services.bot_api.request",
        lambda *_: TelegramResult(True, {"message_id": 900}),
    )
    new = TelegramDelivery.objects.exclude(pk=delivery.pk).get()
    assert deliver_one_notification(new.pk) == 1
    assert deliver_one_notification(new.pk) == 0
    if ambiguous:
        assert deliver_one_notification(delivery.pk) == 0
    else:
        TelegramDelivery.objects.filter(pk=delivery.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        assert deliver_one_notification(delivery.pk) == 1
        delivery.refresh_from_db()
        assert delivery.state == DeliveryState.SENT
