# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import User
from django.db import close_old_connections
from django.test import Client

from apps.clans.governance import _event
from apps.clans.models import ClanRole
from apps.clans.services import decide_request, reconcile_telegram_membership, request_join
from apps.notifications.clan_copy import CLAN_COPY, display_copy
from apps.notifications.models import Notification, NotificationTemplate, TelegramDelivery
from apps.notifications.services import NotificationIdempotencyConflict, emit_notification

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def event(player_factory):
    return dict(
        recipient=player_factory(),
        event_key="CLAN_INVITE_READY",
        source_domain="CLAN",
        source_type="CLAN_INVITE",
        source_id="1",
        idempotency_key="invite-ready",
        payload={"invite_id": 1},
    )


@pytest.mark.parametrize("key", CLAN_COPY)
def test_safe_default_copy_for_important_events(event, key):
    event["event_key"] = key
    first, created = emit_notification(**event)
    assert created and first.title and first.body
    assert "http" not in first.title + first.body
    assert "Clan" in first.title + first.body
    again, created = emit_notification(**event)
    assert not created and again.pk == first.pk
    assert not TelegramDelivery.objects.exists()


def test_latest_enabled_template_precedence_and_disabled_fallback(event):
    actor = User.objects.create_user("copy-editor")
    for version, enabled, title in [
        (1, True, "Old"),
        (2, True, "Configured"),
        (3, False, "Disabled"),
    ]:
        NotificationTemplate.objects.create(
            event_key=event["event_key"],
            channel="IN_APP",
            version=version,
            enabled=enabled,
            title=title,
            body=title + " body",
            created_by=actor,
        )
    n, _ = emit_notification(**event, title="Caller", body="Caller body")
    assert (n.title, n.body) == ("Configured", "Configured body")
    assert display_copy(n) == ("Configured", "Configured body")
    # Existing no-explicit-copy event retries must not adopt a newer template/default as identity.
    assert emit_notification(**event)[0].pk == n.pk
    with pytest.raises(NotificationIdempotencyConflict):
        emit_notification(**{**event, "source_id": "other"})
    NotificationTemplate.objects.filter(event_key=event["event_key"]).update(enabled=False)
    n, _ = emit_notification(**{**event, "idempotency_key": "disabled"})
    assert (n.title, n.body) == CLAN_COPY[event["event_key"]]


def test_legacy_blank_notifications_list_read_and_history(event, init_data_factory, settings):
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    owner = event["recipient"]
    n = Notification.objects.create(**event, title="", body="")
    before = Notification.objects.values().get(pk=n.pk)
    replay, created = emit_notification(**event)
    assert not created and replay.pk == n.pk
    assert Notification.objects.values().get(pk=n.pk) == before
    http = Client(
        HTTP_AUTHORIZATION="tma "
        + init_data_factory(user={"id": owner.telegram_user_id, "first_name": "QA"})
    )
    response = http.get("/api/v1/notifications")
    item = response.json()["items"][0]
    assert item["title"] == "Clan invitation ready"
    assert "Clan Status" in item["body"]
    assert item["deep_link"] == {"path": "/clan"}
    assert item["payload"] == {"invite_id": 1}
    read = http.post(f"/api/v1/notifications/{n.public_id}/read")
    assert read.status_code == 200
    assert read.json()["title"] == item["title"]
    assert read.json()["body"] == item["body"]
    after = Notification.objects.values().get(pk=n.pk)
    assert after["read_at"] is not None
    assert {k: v for k, v in after.items() if k != "read_at"} == {
        k: v for k, v in before.items() if k != "read_at"
    }
    assert Notification.objects.count() == 1


def test_partial_legacy_copy_and_unrelated_event_are_preserved(event):
    n = Notification.objects.create(**event, title="Existing title", body="  ")
    assert display_copy(n) == ("Existing title", CLAN_COPY["CLAN_INVITE_READY"][1])
    n.source_domain = "OTHER"
    assert display_copy(n) == ("Existing title", "")
    n.source_domain, n.event_key = "CLAN", "UNKNOWN_EVENT"
    assert display_copy(n) == ("Existing title", "")


def test_simultaneous_defaults_do_not_change_event_identity(event):
    barrier = Barrier(2)

    def run(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return emit_notification(**event)[0].pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[0] == results[1]
    assert Notification.objects.count() == 1


def test_join_decisions_verified_activation_and_recovery_are_once(clan_world, monkeypatch):
    now, _, _, resolved, make_player, make_clan = clan_world
    cs = make_clan()
    soldier = make_player(ClanRole.SOLDIER)
    req = request_join(soldier, cs, resolved)
    decide_request(cs.captain, req.pk, accept=True)
    decide_request(cs.captain, req.pk, accept=True)
    accepted = Notification.objects.get(event_key="CLAN_JOIN_ACCEPTED", recipient=soldier)
    assert accepted.title and accepted.body
    assert not Notification.objects.filter(
        event_key="CLAN_MEMBERSHIP_ACTIVATED", recipient=soldier
    ).exists()
    monkeypatch.setattr("apps.clans.services.queue_invite_revoke", lambda *a, **k: None)
    req.refresh_from_db()
    assert req.decided_at is not None
    for _ in range(2):
        reconcile_telegram_membership(
            player=soldier,
            group=cs.clan.telegram_group,
            present=True,
            telegram_update_id=123,
            observed_at=req.decided_at,
        )
    active = Notification.objects.get(event_key="CLAN_MEMBERSHIP_ACTIVATED", recipient=soldier)
    assert active.title and active.body
    rejected = make_player(ClanRole.SOLDIER)
    denied = request_join(rejected, cs, resolved)
    decide_request(cs.captain, denied.pk, accept=False)
    decide_request(cs.captain, denied.pk, accept=False)
    assert (
        Notification.objects.filter(event_key="CLAN_JOIN_DECLINED", recipient=rejected).count() == 1
    )
    for _ in range(2):
        _event(cs, "RECOVERY_ENTERED", now, source_type="TEST", source_id="same")
    assert Notification.objects.filter(event_key="CLAN_RECOVERY_ENTERED").count() == 2
    assert not TelegramDelivery.objects.exists()
