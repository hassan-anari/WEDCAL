# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import copy
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.notifications import broadcast_content
from apps.notifications.models import (
    BotAdminOperator,
    BotAdminSession,
    BroadcastCampaign,
    BroadcastRecipient,
    BroadcastSource,
    TelegramDelivery,
)
from apps.notifications.services import (
    cancel_campaign,
    deliver_broadcasts,
    deliver_one_notification,
    materialize_campaign,
)
from apps.notifications.tests.test_b3_bot_admin import callback, message, process, token
from apps.notifications.tests.test_services import player
from apps.telegram.bot_api import TelegramResult
from apps.telegram.models import TelegramGroup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def media_world(settings, monkeypatch):
    settings.B3_BROADCAST_AUDIENCES_ENABLED = True
    settings.TELEGRAM_ADMIN_IDS = {101}
    settings.TELEGRAM_BOT_ID = 123456
    settings.BROADCAST_ALBUM_QUIET_SECONDS = 0
    actor = User.objects.create_user("media-operator")
    actor.user_permissions.add(Permission.objects.get(codename="manage_broadcasts"))
    operator = BotAdminOperator.objects.create(user=actor, telegram_user_id=101, enabled=True)
    player(201)
    monkeypatch.setattr(
        "apps.notifications.tasks.deliver_one_notification.apply_async", lambda **_: None
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: pytest.fail("Never real Telegram")
    )
    calls: list[object] = []

    def native(method, parameters):
        calls.append((method, parameters))
        if method == "copyMessages":
            return TelegramResult(
                True, {"message_ids": [1000 + i for i in parameters["message_ids"]]}
            )
        if method == "getChatMember":
            return TelegramResult(
                True, {"user": {"id": settings.TELEGRAM_BOT_ID}, "status": "administrator"}
            )
        return TelegramResult(True, {"message_id": 999})

    monkeypatch.setattr("apps.telegram.bot_api.request", native)
    return actor, operator, calls


def record_result(calls, args, result):
    calls.append(args)
    return result


def start_composer():
    process(message(1))
    process(callback(2, token("compose")))


def content(kind="photo", update_id=3, album=None):
    value = message(update_id, text="Exact <b>literal</b> 👋")
    raw = value["message"]
    if kind == "formatted":
        raw["entities"] = [{"type": "bold", "offset": 0, "length": 5}]
    elif kind != "text":
        raw.pop("text")
        raw[kind] = (
            [{"file_id": "sensitive-synthetic-id"}]
            if kind == "photo"
            else {"file_id": "sensitive-synthetic-id"}
        )
        raw["caption"] = "Caption 👋"
        raw["caption_entities"] = [{"type": "bold", "offset": 0, "length": 7}]
    if album:
        raw["media_group_id"] = album
    return value


def preview_and_confirm(update_id=20):
    process(callback(update_id, token("users")))
    campaign = BroadcastCampaign.objects.get()
    preview = TelegramDelivery.objects.get(
        notification__idempotency_key=f"bot-panel:{update_id}:preview"
    )
    deliver_one_notification(preview.pk)
    process(callback(update_id + 1, token("confirm")))
    campaign.refresh_from_db()
    assert campaign.state == "MATERIALIZING"
    materialize_campaign(campaign.pk)
    return campaign


@pytest.mark.parametrize(
    "kind",
    [
        "text",
        "formatted",
        "photo",
        "video",
        "voice",
        "audio",
        "document",
        "animation",
        "video_note",
        "sticker",
        "location",
        "contact",
        "dice",
    ],
)
def test_native_original_preview_and_delivery_preserves_message_reference(media_world, kind):
    _, _, calls = media_world
    start_composer()
    original = content(kind)
    process(original)
    row = preview_and_confirm()
    assert row.copy_source.state == "READY"
    assert row.copy_source.messages[0]["id"] == original["message"]["message_id"]
    if kind == "formatted":
        assert row.copy_source.messages[0]["entities"] == original["message"]["entities"]
    assert "file_id" not in str(row.copy_source.messages)
    calls.clear()
    deliver_broadcasts()
    assert all(method == "copyMessage" for method, _ in calls)
    assert {p["chat_id"] for _, p in calls} == {101, 201}
    assert all(p["from_chat_id"] == 101 and p["message_id"] == 3 for _, p in calls)
    assert row.recipients.filter(state="SENT").count() == 2
    calls.clear()
    deliver_broadcasts()
    assert calls == []
    with pytest.raises(DatabaseError), transaction.atomic():
        BroadcastSource.objects.filter(pk=row.copy_source_id).update(messages=[])
    with pytest.raises(DatabaseError), transaction.atomic():
        BroadcastCampaign.objects.filter(pk=row.pk).update(copy_source=None)


@pytest.mark.parametrize(
    "kinds", [("photo", "video"), ("document", "document"), ("audio", "audio")]
)
def test_album_complete_count_sorting_native_group_preview_and_delivery(media_world, kinds):
    _, _, calls = media_world
    start_composer()
    # Intentionally reversed update processing; durable album ordering uses message IDs.
    process(content(kinds[1], 4, "synthetic-album"))
    process(content(kinds[0], 3, "synthetic-album"))
    assert BotAdminSession.objects.get().step == "COLLECTING"
    process(message(5, text="/done 3"))
    assert BotAdminSession.objects.get().step == "COLLECTING"
    process(message(6, text="/done 2"))
    assert BotAdminSession.objects.get().step == "AUDIENCE"
    row = preview_and_confirm()
    assert [m["id"] for m in row.copy_source.messages] == [3, 4]
    calls.clear()
    deliver_broadcasts()
    assert len(calls) == 2 and all(method == "copyMessages" for method, _ in calls)
    assert all(p["message_ids"] == [3, 4] for _, p in calls)
    assert all(r.telegram_message_ids == [1003, 1004] for r in row.recipients.all())


def test_album_early_finish_pending_parts_oversize_protection_and_expiration(media_world, settings):
    _, operator, _ = media_world
    settings.BROADCAST_ALBUM_QUIET_SECONDS = 30
    source = broadcast_content.capture(operator, content("photo", 3, "early")["message"])
    broadcast_content.capture(operator, content("photo", 4, "early")["message"])
    with pytest.raises(ValidationError):
        broadcast_content.seal_album(source.pk, expected_count=2)
    BroadcastSource.objects.filter(pk=source.pk).update(
        quiet_until=timezone.now() - timedelta(seconds=1),
        deadline=timezone.now() - timedelta(seconds=1),
    )
    with pytest.raises(ValidationError):
        broadcast_content.seal_album(source.pk, expected_count=2)
    for i in range(10):
        source = broadcast_content.capture(
            operator, content("photo", i + 10, "oversize")["message"]
        )
    source = broadcast_content.capture(operator, content("photo", 25, "oversize")["message"])
    assert source.state == "INVALID"
    protected = content()["message"]
    protected["has_protected_content"] = True
    with pytest.raises(ValidationError):
        broadcast_content.capture(operator, protected)
    for kind in ["invoice", "paid_media", "new_chat_members"]:
        with pytest.raises(ValidationError):
            broadcast_content.capture(operator, content(kind)["message"])


def test_partial_preview_blocks_confirmation_and_late_album_part_invalidates_source(
    media_world, monkeypatch
):
    _, _, _ = media_world
    start_composer()
    process(content("photo", 3, "partial"))
    process(content("photo", 4, "partial"))
    process(message(5, text="/done 2"))
    process(callback(6, token("users")))
    preview = TelegramDelivery.objects.get(notification__idempotency_key="bot-panel:6:preview")
    monkeypatch.setattr(
        "apps.telegram.bot_api.request", lambda *_: TelegramResult(True, {"message_ids": [50]})
    )
    deliver_one_notification(preview.pk)
    preview.refresh_from_db()
    assert preview.state == "UNCERTAIN" and preview.error_category == "partial_copy"
    assert preview.telegram_message_ids == [50]
    process(callback(7, token("confirm")))
    assert BroadcastCampaign.objects.get().state == "DRAFT"
    process(content("photo", 8, "partial"))
    assert BroadcastSource.objects.get().state == "INVALID"


@pytest.mark.parametrize(
    "result,state",
    [
        (TelegramResult(False, category="source_unavailable"), "FAILED"),
        (TelegramResult(False, category="timeout", ambiguous=True), "UNCERTAIN"),
        (TelegramResult(False, category="forbidden"), "SKIPPED"),
        (TelegramResult(True, {"message_ids": [700]}), "UNCERTAIN"),
    ],
)
def test_album_failure_and_partial_delivery_never_blindly_retry(
    media_world, monkeypatch, result, state
):
    _, _, _ = media_world
    start_composer()
    process(content("photo", 3, "delivery"))
    process(content("photo", 4, "delivery"))
    process(message(5, text="/done 2"))
    campaign = preview_and_confirm()
    calls: list[object] = []
    monkeypatch.setattr(
        "apps.telegram.bot_api.request", lambda *args: record_result(calls, args, result)
    )
    deliver_broadcasts()
    assert campaign.recipients.filter(state=state).count() == 2
    before = len(calls)
    deliver_broadcasts()
    assert len(calls) == before


def test_rate_refusal_retries_once_and_cancellation_stops_remaining_work(media_world, monkeypatch):
    actor, _, _ = media_world
    start_composer()
    process(content("voice"))
    campaign = preview_and_confirm()
    monkeypatch.setattr(
        "apps.telegram.bot_api.request",
        lambda *_: TelegramResult(False, category="rate_limited", retry_after=1),
    )
    deliver_broadcasts()
    assert campaign.recipients.filter(state="RETRY_WAIT", attempt_count=0).count() == 2
    BroadcastRecipient.objects.filter(campaign=campaign).update(
        next_attempt_at=timezone.now() - timedelta(seconds=1)
    )
    calls: list[object] = []
    monkeypatch.setattr(
        "apps.telegram.bot_api.request",
        lambda *args: record_result(calls, args, TelegramResult(True, {"message_id": 5})),
    )
    deliver_broadcasts(limit=1)
    cancel_campaign(campaign=campaign, actor=actor, reason="Cancel unprocessed")
    deliver_broadcasts()
    assert len(calls) == 1 and campaign.recipients.filter(state="SENT").count() == 1
    assert campaign.recipients.filter(state="SKIPPED").count() == 1


def test_known_group_revalidation_and_revoked_operator(media_world, monkeypatch):
    actor, operator, calls = media_world
    start_composer()
    process(content("document"))
    campaign = preview_and_confirm()
    now = timezone.now()
    group = TelegramGroup.objects.create(
        telegram_chat_id=-300,
        chat_type="supergroup",
        bot_is_admin=True,
        bot_membership_status="administrator",
        first_seen_at=now,
        last_seen_at=now,
    )
    recipient = BroadcastRecipient.objects.create(
        campaign=campaign, target_kind="GROUP", group=group, telegram_user_id=-300
    )
    monkeypatch.setattr(
        "apps.telegram.bot_api.get_chat_member",
        lambda *_: TelegramResult(True, {"user": {"id": 123456}, "status": "member"}),
    )
    deliver_broadcasts(limit=2)
    recipient.refresh_from_db()
    assert (
        recipient.state == "SKIPPED" and recipient.error_category == "group_permission_unavailable"
    )
    operator.enabled = False
    operator.save()
    deliver_broadcasts()
    assert campaign.recipients.filter(error_category="operator_authorization_revoked").count() == 1


def test_unauthorized_original_cannot_replace_source_and_no_sensitive_logs(media_world, caplog):
    _, operator, _ = media_world
    start_composer()
    raw = content("photo")
    for defect in ["group", "wrong_user", "unauthenticated", "forwarded"]:
        bad = copy.deepcopy(raw)
        if defect == "group":
            bad["message"]["chat"]["type"] = "supergroup"
        if defect == "wrong_user":
            bad["message"]["from"]["id"] = 999
        if defect == "forwarded":
            bad["message"]["forward_origin"] = {"type": "user"}
        process(bad, authenticated=defect != "unauthenticated")
        raw["update_id"] += 1
    assert not BroadcastSource.objects.exists()
    assert "sensitive-synthetic-id" not in caplog.text
