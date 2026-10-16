# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, transaction

from apps.notifications import announcements
from apps.notifications.models import (
    InAppAnnouncement,
    Notification,
    NotificationTemplate,
    TelegramDelivery,
)
from apps.notifications.tests.test_services import player

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def announcement_world(monkeypatch):
    monkeypatch.setattr(
        "apps.telegram.bot_api.request",
        lambda *_: pytest.fail("No Telegram from In-App publishing"),
    )
    actor = User.objects.create_superuser("announcement-owner")
    template = NotificationTemplate.objects.create(
        event_key="news",
        channel="IN_APP",
        version=1,
        title="Exact 👋 title",
        body="Body\nwith lines <b>literal</b>",
        created_by=actor,
    )
    recipients = [player(300 + i) for i in range(5)]
    row = InAppAnnouncement.objects.create(template=template, audience="ALL", created_by=actor)
    return actor, template, recipients, row


def publish(actor, row):
    return announcements.confirm(
        row.pk, actor, expected=announcements.preview(row), reason="Isolated test confirmation"
    )


def test_bounded_exactly_once_snapshot_and_existing_recipient_fence(announcement_world):
    actor, template, recipients, row = announcement_world
    row = publish(actor, row)
    # A later template for the same event must not override the confirmed content.
    NotificationTemplate.objects.create(
        event_key="IN_APP_ANNOUNCEMENT",
        channel="IN_APP",
        version=9,
        title="Wrong latest",
        body="Do not replace snapshot",
        created_by=actor,
    )
    late = player(900)
    assert announcements.process_batch(row.pk, limit=2) == 2
    assert Notification.objects.count() == 2
    assert announcements.process_batch(row.pk, limit=2) == 2
    assert announcements.process_batch(row.pk, limit=2) == 1
    assert announcements.process_batch(row.pk) == 0
    row.refresh_from_db()
    assert row.state == "COMPLETED" and row.processed_count == row.estimated_recipients == 5
    assert set(Notification.objects.values_list("recipient_id", flat=True)) == {
        p.pk for p in recipients
    }
    assert not Notification.objects.filter(recipient=late).exists()
    assert set(Notification.objects.values_list("title", "body", "template_id")) == {
        (template.title, template.body, template.pk)
    }
    assert not TelegramDelivery.objects.exists()
    assert announcements.confirm(row.pk, actor, expected={}, reason="Replay").state == "COMPLETED"
    with pytest.raises(DatabaseError), transaction.atomic():
        InAppAnnouncement.objects.filter(pk=row.pk).update(body_snapshot="Rewrite")
    with pytest.raises(DatabaseError), transaction.atomic():
        NotificationTemplate.objects.filter(pk=template.pk).update(body="Rewrite")


def test_one_player_cancel_and_no_cross_player_center_leak(
    announcement_world, client, init_data_factory, settings
):
    actor, _, recipients, row = announcement_world
    row.audience, row.player = "ONE", recipients[0]
    row.save()
    publish(actor, row)
    assert announcements.process_batch(row.pk) == 1
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    for recipient, count in [(recipients[0], 1), (recipients[1], 0)]:
        response = client.get(
            "/api/v1/notifications",
            headers={
                "Authorization": "tma "
                + init_data_factory(
                    user={"id": recipient.telegram_user_id, "first_name": "Isolated"}
                )
            },
        )
        assert response.status_code == 200 and len(response.json()["items"]) == count
    other = InAppAnnouncement.objects.create(
        template=row.template, audience="ALL", created_by=actor
    )
    publish(actor, other)
    announcements.process_batch(other.pk, limit=2)
    announcements.cancel(other.pk, actor, reason="Stop remaining")
    assert announcements.process_batch(other.pk) == 0
    other.refresh_from_db()
    assert other.state == "CANCELLED" and other.processed_count == 2
    assert not TelegramDelivery.objects.exists()


def test_permissions_preview_confirmation_and_no_request_materialization(
    announcement_world, client
):
    actor, _, _, row = announcement_world
    staff = User.objects.create_user("draft-author", is_staff=True)
    staff.user_permissions.add(
        *Permission.objects.filter(
            codename__in=[
                "view_inappannouncement",
                "add_inappannouncement",
                "change_inappannouncement",
            ]
        )
    )
    client.force_login(staff)
    url = f"/admin/notifications/inappannouncement/{row.pk}/publish/"
    response = client.get(url)
    assert response.status_code == 200 and "Exact 👋 title" in response.content.decode()
    signature = response.context["confirmation"]
    assert (
        client.post(
            url,
            {
                "operation": "publish",
                "confirmation": signature,
                "confirm": "yes",
                "reason": "No permission",
            },
        ).status_code
        == 403
    )
    with pytest.raises(PermissionDenied):
        announcements.cancel(row.pk, staff, reason="No permission")
    client.force_login(actor)
    response = client.get(url)
    token = response.context["confirmation"]
    assert (
        client.post(
            url, {"operation": "publish", "confirmation": "fake", "confirm": "yes", "reason": "bad"}
        ).status_code
        == 200
    )
    assert not Notification.objects.exists()
    response = client.post(
        url,
        {
            "operation": "publish",
            "confirmation": token,
            "confirm": "yes",
            "reason": "Confirmed exact preview",
        },
    )
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.state == "PENDING" and not Notification.objects.exists()
    assert not TelegramDelivery.objects.exists()


def test_draft_changes_reject_stale_confirmation_and_telegram_templates(announcement_world):
    actor, template, recipients, row = announcement_world
    expected = announcements.preview(row)
    row.audience, row.player = "ONE", recipients[0]
    row.save()
    with pytest.raises(ValidationError):
        announcements.confirm(row.pk, actor, expected=expected, reason="Stale preview")
    template.channel = "TELEGRAM"
    row.template = template
    with pytest.raises(ValidationError):
        row.clean()


def test_batch_failure_rolls_back_cursor_and_notifications_then_retries(
    announcement_world, monkeypatch, settings
):
    actor, _, _, row = announcement_world
    publish(actor, row)
    original = announcements.emit_notification
    calls = 0

    def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic transaction failure")
        return original(**kwargs)

    monkeypatch.setattr(announcements, "emit_notification", fail_second)
    assert announcements.process_batch(row.pk, limit=3) == 0
    assert Notification.objects.count() == 0
    row.refresh_from_db()
    assert row.cursor == row.processed_count == 0 and row.attempt_count == 1
    InAppAnnouncement.objects.filter(pk=row.pk).update(next_attempt_at=None)
    monkeypatch.setattr(announcements, "emit_notification", original)
    assert announcements.process_batch(row.pk) == 5
    assert Notification.objects.count() == 5


def test_concurrent_workers_and_cancel_serialize(announcement_world):
    actor, _, _, row = announcement_world
    publish(actor, row)
    barrier = Barrier(2)

    def run(_):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            return announcements.process_batch(row.pk, limit=2)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        sum(pool.map(run, range(2)))
    while announcements.process_batch(row.pk, limit=2):
        pass
    row.refresh_from_db()
    assert row.processed_count == Notification.objects.count() == 5
