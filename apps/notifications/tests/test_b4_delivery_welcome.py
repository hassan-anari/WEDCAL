# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import RequestFactory
from ninja.errors import HttpError

from apps.notifications.api import list_notifications, read_all, read_notification
from apps.notifications.models import (
    BotAdminOperator,
    BroadcastCampaign,
    Notification,
    OperatorAuditEvent,
    TelegramDelivery,
    WelcomeConfiguration,
    WelcomeTemplate,
)
from apps.notifications.services import (
    deliver_notifications,
    deliver_one_notification,
    queue_campaign,
)
from apps.notifications.tasks import deliver_one_notification as delivery_task
from apps.notifications.tests.test_services import authorized_operator, player
from apps.notifications.welcome_config import activate_welcome, validate_template
from apps.notifications.welcome_config import test_send_welcome as queue_test
from apps.telegram.bot_api import TelegramResult
from apps.telegram.welcome import queue_private_message, queue_welcome

pytestmark = pytest.mark.django_db(transaction=True)
KEYBOARD = {
    "inline_keyboard": [
        [{"text": "🎮 Play", "web_app": {"url": "https://dalexio-game.vercel.app/"}}],
        [{"text": "Community", "url": "https://t.me/Dalexio_Official"}],
    ]
}


@pytest.fixture(autouse=True)
def no_real_broker_or_telegram(monkeypatch):
    published = []
    monkeypatch.setattr(delivery_task, "apply_async", lambda **kwargs: published.append(kwargs))

    def forbidden(*args, **kwargs):
        raise AssertionError("B4 tests must not contact real Telegram users")

    monkeypatch.setattr("apps.telegram.bot_api.request", forbidden)
    monkeypatch.setattr("apps.telegram.bot_api.send_message", forbidden)
    return published


def test_after_commit_dispatch_rollback_broker_failure_and_scanner(
    no_real_broker_or_telegram, monkeypatch
):
    recipient = player()
    with transaction.atomic():
        row = queue_private_message(recipient, key="isolated-interactive", text="Synthetic")
        assert not no_real_broker_or_telegram
    assert no_real_broker_or_telegram == [{"args": [row.pk], "countdown": 0}]
    with pytest.raises(ValueError), transaction.atomic():
        queue_private_message(recipient, key="rolled-back", text="Never send")
        raise ValueError("rollback")
    assert TelegramDelivery.objects.count() == 1
    assert len(no_real_broker_or_telegram) == 1
    calls = []

    def send(*args):
        assert not connection.in_atomic_block
        calls.append(args)
        return TelegramResult(True, {"message_id": 5})

    monkeypatch.setattr("apps.telegram.bot_api.send_message", send)
    assert deliver_one_notification(row.pk) == 1
    assert deliver_one_notification(row.pk) == 0 and len(calls) == 1

    def fail(**kwargs):
        raise RuntimeError("isolated broker outage")

    monkeypatch.setattr(delivery_task, "apply_async", fail)
    recovery = queue_private_message(recipient, key="recover-via-scanner", text="Durable")
    assert deliver_notifications() == 1
    recovery.refresh_from_db()
    assert recovery.state == "SENT" and len(calls) == 2


def test_concurrent_delivery_claim_is_single_send(monkeypatch):
    row = queue_private_message(player(), key="single-send-race", text="Synthetic")
    barrier = Barrier(2)
    calls = []

    def send(*args):
        assert not connection.in_atomic_block
        calls.append(args)
        return TelegramResult(True, {"message_id": 8})

    monkeypatch.setattr("apps.telegram.bot_api.send_message", send)

    def run(_):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            return deliver_one_notification(row.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(run, range(2))) == 1
    assert len(calls) == 1


def test_local_budget_retry_dispatches_due_task_without_consuming_attempt(
    monkeypatch, no_real_broker_or_telegram
):
    row = queue_private_message(player(), key="retry", text="Synthetic")
    monkeypatch.setattr(
        "apps.telegram.bot_api.send_message",
        lambda *_: TelegramResult(False, category="local_rate_limited", retry_after=1),
    )
    deliver_one_notification(row.pk)
    row.refresh_from_db()
    assert row.state == "RETRY_WAIT" and row.attempt_count == 0
    assert no_real_broker_or_telegram[-1]["args"] == [row.pk]
    assert no_real_broker_or_telegram[-1]["countdown"] > 0


def test_permission_revocation_prevents_private_admin_send(settings):
    settings.B3_BROADCAST_AUDIENCES_ENABLED = True
    settings.TELEGRAM_ADMIN_IDS = {100}
    user = authorized_operator("operator")
    BotAdminOperator.objects.create(user=user, telegram_user_id=100, enabled=True)
    row = queue_private_message(player(), key="bot-panel:123", text="Privileged preview")
    user.is_active = False
    user.save()
    assert deliver_one_notification(row.pk) == 1
    row.refresh_from_db()
    assert row.state == "SKIPPED" and row.error_category == "operator_authorization_revoked"
    unauthorized = User.objects.create_user("unauthorized")
    campaign = BroadcastCampaign.objects.create(
        created_by=unauthorized,
        audience_type="ALL",
        body_snapshot="No send",
        purpose="authorization regression",
    )
    with pytest.raises(PermissionDenied):
        queue_campaign(campaign=campaign, actor=unauthorized, reason="not authorized")
    campaign.refresh_from_db()
    assert campaign.state == "DRAFT"


def test_notification_links_are_server_owned_and_read_state_is_scoped():
    recipient, other = player(100), player(101)
    row = Notification.objects.create(
        recipient=recipient,
        event_key="CLAN_ACCESS_LOST",
        source_domain="CLAN",
        source_type="TEST",
        source_id="1",
        idempotency_key="safe-link",
        title="Access changed",
        payload={"url": "javascript:alert(1)", "deep_link": "https://attacker.example"},
    )
    Notification.objects.create(
        recipient=recipient,
        event_key="UNKNOWN",
        source_domain="unknown",
        source_type="TEST",
        source_id="2",
        idempotency_key="unsafe-link",
        payload={"url": "https://attacker.example"},
    )
    request = RequestFactory().get("/api/v1/notifications")
    request.auth = recipient  # type: ignore[attr-defined]
    page = list_notifications(request, limit=1)
    assert page.unread_count == 2 and page.next_cursor
    assert page.items[0].deep_link is None
    older = list_notifications(request, cursor=page.next_cursor)
    assert older.items[0].deep_link is not None
    assert older.items[0].deep_link.path == "/clan"
    read_notification(request, row.public_id)
    read_notification(request, row.public_id)
    assert list_notifications(request).unread_count == 1
    request.auth = other  # type: ignore[attr-defined]
    assert list_notifications(request).items == []
    with pytest.raises(HttpError):
        read_notification(request, row.public_id)
    request.auth = recipient  # type: ignore[attr-defined]
    assert read_all(request).updated_count == 1
    assert read_all(request).updated_count == 0


@pytest.mark.parametrize(
    "body",
    [
        "<script>alert(1)</script>",
        "<b>unclosed",
        "<b>x</i>",
        '<a href="javascript:alert(1)">x</a>',
        '<b onclick="bad()">x</b>',
        "{username}",
        "&bogus;",
        "<!--x-->",
        "<b/>",
        "<bad",
        "x\x00y",
        "a" * 4097,
    ],
)
def test_welcome_html_rejects_unsafe_templates(body):
    with pytest.raises(ValidationError):
        validate_template(body, KEYBOARD)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://t.me/example",
        "https://t.me@evil.example/",
        "https://t.me.evil.example/",
        "https://t.me/x%0a",
        "https://t.me/share/url?url=https://evil.example",
        "https://dalexio-game.vercel.app/?redirect=https://evil.example",
        "https://t.me:444/a",
        "https://t.me/../a",
        "https://t.me/a#frag",
    ],
)
def test_welcome_buttons_reject_unsafe_urls(url):
    with pytest.raises(ValidationError):
        validate_template(
            "Hello 👋 <b>Explorer</b>", {"inline_keyboard": [[{"text": "Open", "url": url}]]}
        )


def test_welcome_activation_rollback_deduplication_and_operator_only_test_send(settings):
    actor = User.objects.create_superuser("welcome-owner")
    first = WelcomeTemplate.objects.create(
        version=1, body="Hello 👋 <b>Explorer</b>", keyboard=KEYBOARD, created_by=actor
    )
    second = WelcomeTemplate.objects.create(
        version=2, body="New <i>journey</i>", keyboard=KEYBOARD, created_by=actor
    )
    recipient = player()
    activate_welcome(first, actor, reason="Review only")
    assert not TelegramDelivery.objects.exists()
    settings.B3_BOT_WELCOME_ENABLED = False
    queue_welcome(recipient)
    assert not TelegramDelivery.objects.exists()
    settings.B3_BOT_WELCOME_ENABLED = True
    settings.B4_PUBLIC_WELCOME_ENABLED = True  # Explicit synthetic activation only.
    queue_welcome(recipient)
    activate_welcome(second, actor, reason="New approved version")
    queue_welcome(recipient)
    assert TelegramDelivery.objects.count() == 1
    assert TelegramDelivery.objects.get().text_snapshot == first.body
    activate_welcome(first, actor, reason="Rollback")
    assert WelcomeConfiguration.objects.get().active_template == first
    assert OperatorAuditEvent.objects.filter(action="WELCOME_ACTIVATE").count() == 3
    with pytest.raises(PermissionDenied):
        queue_test(first, actor, reason="No arbitrary recipient allowed", request_key="blocked")
    settings.TELEGRAM_ADMIN_IDS = {recipient.telegram_user_id}
    BotAdminOperator.objects.create(
        user=actor, telegram_user_id=recipient.telegram_user_id, enabled=True
    )
    queue_test(second, actor, reason="Operator preview", request_key="same-confirmation")
    queue_test(second, actor, reason="Operator preview replay", request_key="same-confirmation")
    assert TelegramDelivery.objects.count() == 2
    test = TelegramDelivery.objects.get(notification__idempotency_key__startswith="welcome-test:")
    assert test.telegram_user_id == recipient.telegram_user_id and test.text_snapshot == second.body
    with pytest.raises(ValidationError):
        first.body = "Rewrite"
        first.save()
    with pytest.raises(DatabaseError), transaction.atomic():
        WelcomeTemplate.objects.filter(pk=first.pk).update(body="DB rewrite")
    with pytest.raises(DatabaseError), transaction.atomic():
        WelcomeTemplate.objects.filter(pk=first.pk).delete()


def test_welcome_admin_preview_get_and_invalid_confirmation_send_nothing(client):
    actor = User.objects.create_superuser("admin-preview", password="synthetic")
    template = WelcomeTemplate.objects.create(
        version=1, body="Hello <b>Explorer</b>", keyboard=KEYBOARD, created_by=actor
    )
    client.force_login(actor)
    assert client.get("/admin/notifications/welcometemplate/add/").status_code == 200
    assert (
        client.get(f"/admin/notifications/welcometemplate/{template.pk}/change/").status_code == 200
    )
    base = f"/admin/notifications/welcometemplate/{template.pk}/control"
    for action in ["preview", "activate", "test"]:
        assert client.get(f"{base}/{action}/").status_code == 200
    assert not TelegramDelivery.objects.exists() and not WelcomeConfiguration.objects.exists()
    response = client.post(
        f"{base}/activate/", {"confirm": "yes", "reason": "unsigned", "confirmation": "fake"}
    )
    assert response.status_code == 200 and not WelcomeConfiguration.objects.exists()


def test_old_b3_flag_alone_cannot_activate_public_dynamic_welcome(settings):
    actor = User.objects.create_superuser("welcome-gate")
    template = WelcomeTemplate.objects.create(
        version=1, body="Hello", keyboard=KEYBOARD, created_by=actor
    )
    activate_welcome(template, actor, reason="Configuration only")
    settings.B3_BOT_WELCOME_ENABLED = True
    settings.B4_PUBLIC_WELCOME_ENABLED = False
    queue_welcome(player())
    assert not TelegramDelivery.objects.exists()
