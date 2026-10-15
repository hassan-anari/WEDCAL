# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import copy
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import Permission, User
from django.utils import timezone

from apps.notifications.models import (
    BotAdminInteraction,
    BotAdminOperator,
    BotAdminSession,
    BroadcastCampaign,
    OperatorAuditEvent,
    TelegramDelivery,
)
from apps.players.models import Player
from apps.telegram.services import persist_inbound_update, process_inbound_update
from apps.telegram.welcome import WELCOME, keyboard

pytestmark = pytest.mark.django_db(transaction=True)


def message(update_id=1, user_id=101, text="/admin", chat_type="private", chat_id=None):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(timezone.now().timestamp()),
            "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "Operator"},
            "text": text,
        },
    }


def process(payload, authenticated=True):
    inbound, _ = persist_inbound_update(payload, authenticated=authenticated)
    process_inbound_update(inbound.pk)
    return inbound


def callback(update_id, token, user_id=101, chat_id=None, chat_type="private"):
    payload = message(update_id, user_id, chat_type=chat_type, chat_id=chat_id)
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": payload["message"]["from"],
            "message": payload["message"],
            "data": f"b3:{token.hex}",
        },
    }


def token(action):
    session = BotAdminSession.objects.get()
    return BotAdminInteraction.objects.get(
        session=session, revision=session.revision, action=action
    ).token


@pytest.fixture
def operator(settings, monkeypatch):
    settings.TELEGRAM_ADMIN_IDS = frozenset({101})
    settings.B3_BROADCAST_AUDIENCES_ENABLED = True
    user = User.objects.create_user("bot-operator")
    user.user_permissions.add(Permission.objects.get(codename="manage_broadcasts"))
    row = BotAdminOperator.objects.create(user=user, telegram_user_id=101, enabled=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("Ingress must never send live Telegram messages")

    monkeypatch.setattr("apps.telegram.bot_api.request", forbidden)
    return row


def test_full_private_panel_draft_preview_confirm_progress_cancel_and_replay(operator):
    process(message())
    process(callback(2, token("compose")))
    process(message(3, text="Exact plain text <b>stays literal</b>"))
    process(callback(4, token("both")))
    campaign = BroadcastCampaign.objects.get()
    assert campaign.state == "DRAFT" and campaign.audience_type == "BOTH"
    preview = TelegramDelivery.objects.get(notification__idempotency_key="bot-panel:4:preview")
    assert preview.text_snapshot == campaign.body_snapshot and preview.message_options == {}
    confirmation = token("confirm")
    process(callback(5, confirmation))
    campaign.refresh_from_db()
    assert campaign.state == "MATERIALIZING"
    audits = OperatorAuditEvent.objects.filter(action="BROADCAST_QUEUE").count()
    process(callback(6, confirmation))
    assert OperatorAuditEvent.objects.filter(action="BROADCAST_QUEUE").count() == audits == 1
    process(callback(7, token("track")))
    process(callback(8, token("cancel")))
    campaign.refresh_from_db()
    assert campaign.state == "CANCELLED"
    process(message(9, text="/admin"))
    assert BotAdminSession.objects.get().step == "MENU"


@pytest.mark.parametrize(
    "defect",
    [
        "no_allowlist",
        "no_role",
        "disabled",
        "inactive",
        "group",
        "mismatch",
        "bot",
        "forwarded",
        "anonymous",
        "unpersisted_auth",
    ],
)
def test_every_message_requires_complete_authorization(operator, settings, defect):
    payload = message()
    if defect == "no_allowlist":
        settings.TELEGRAM_ADMIN_IDS = frozenset()
    if defect == "no_role":
        operator.user.user_permissions.clear()
    if defect == "disabled":
        operator.enabled = False
        operator.save()
    if defect == "inactive":
        operator.user.is_active = False
        operator.user.save()
    if defect == "group":
        payload["message"]["chat"] = {"id": -100, "type": "supergroup"}
    if defect == "mismatch":
        payload["message"]["chat"]["id"] = 999
    if defect == "bot":
        payload["message"]["from"]["is_bot"] = True
    if defect == "forwarded":
        payload["message"]["forward_origin"] = {"type": "user"}
    if defect == "anonymous":
        payload["message"]["sender_chat"] = {"id": -100}
    process(payload, authenticated=defect != "unpersisted_auth")
    assert not BotAdminSession.objects.exists() and not BroadcastCampaign.objects.exists()
    assert not TelegramDelivery.objects.exists()


@pytest.mark.parametrize(
    "defect", ["other_actor", "group", "mismatch", "expired", "revoked_role", "replayed", "unknown"]
)
def test_callbacks_cannot_be_stolen_expired_or_replayed(operator, defect):
    process(message())
    original = token("compose")
    payload = callback(2, original)
    if defect == "other_actor":
        payload["callback_query"]["from"]["id"] = 102
    if defect == "group":
        payload["callback_query"]["message"]["chat"] = {"id": -100, "type": "supergroup"}
    if defect == "mismatch":
        payload["callback_query"]["message"]["chat"]["id"] = 102
    if defect == "expired":
        BotAdminSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    if defect == "revoked_role":
        operator.user.user_permissions.clear()
    if defect == "replayed":
        BotAdminInteraction.objects.filter(token=original).update(consumed_at=timezone.now())
    if defect == "unknown":
        payload["callback_query"]["data"] = "b3:00000000000000000000000000000000"
    process(payload)
    assert not BroadcastCampaign.objects.exists()


def test_restart_recovery_abandon_and_expired_confirmation(operator):
    process(message())
    process(callback(2, token("compose")))
    process(message(3, text="Recoverable draft"))
    process(message(4, text="/admin"))
    process(callback(5, token("audience")))
    process(callback(6, token("users")))
    confirm = token("confirm")
    BotAdminSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    process(callback(7, confirm))
    assert BroadcastCampaign.objects.get().state == "DRAFT"
    process(message(8, text="/admin"))
    process(callback(9, token("audience")))
    process(callback(10, token("abandon")))
    assert BotAdminSession.objects.get().campaign is None
    assert BroadcastCampaign.objects.get().state == "DRAFT"


def test_private_start_deduplication_welcome_html_and_keyboard(settings):
    settings.B3_BOT_WELCOME_ENABLED = True
    payload = message(text="/start")
    process(payload)
    process(copy.deepcopy(payload))
    process(message(2, text="/start"))
    assert Player.objects.count() == 1 and TelegramDelivery.objects.count() == 1
    delivery = TelegramDelivery.objects.get()
    assert delivery.text_snapshot == WELCOME and len(WELCOME) < 4096
    assert delivery.message_options["parse_mode"] == "HTML"
    options: dict = keyboard()  # type: ignore[type-arg]
    buttons = options["inline_keyboard"][0]
    assert buttons[0]["web_app"]["url"] == "https://dalexio-game.vercel.app/"
    assert buttons[1]["url"] == "https://t.me/Dalexio_Official"
    if os.getenv("B3_EXPORT_FIXTURES"):
        Path("docs/fixtures/b3/start-welcome.json").write_text(
            json.dumps(
                {"text": WELCOME, "options": delivery.message_options}, ensure_ascii=False, indent=2
            )
            + "\n"
        )


def test_group_start_never_delivers_private_welcome():
    process(message(text="/start", chat_type="group", chat_id=-100))
    assert not TelegramDelivery.objects.exists()


def test_default_disabled_rollout_switches_do_not_queue_or_open_panel(settings):
    from django.core.exceptions import ValidationError

    from apps.notifications.services import queue_campaign

    settings.B3_BOT_WELCOME_ENABLED = False
    settings.B3_BROADCAST_AUDIENCES_ENABLED = False
    process(message(text="/start"))
    assert not TelegramDelivery.objects.exists()
    actor = User.objects.create_user("disabled-rollout")
    row = BroadcastCampaign.objects.create(
        created_by=actor, audience_type="BOTH", body_snapshot="blocked", purpose="test"
    )
    with pytest.raises(ValidationError):
        queue_campaign(campaign=row, actor=actor, reason="test")
    row.refresh_from_db()
    assert row.state == "DRAFT"


def test_webhook_secret_is_required_before_persisted_admin_callback(
    operator, client, settings, monkeypatch
):
    from apps.telegram.models import TelegramInboundUpdate

    settings.TELEGRAM_WEBHOOK_SECRET = "synthetic-webhook-secret"
    monkeypatch.setattr("apps.telegram.api.dispatch_inbound_update", lambda *_: None)
    body = json.dumps(message())
    denied = client.post(
        "/api/v1/telegram/webhook",
        data=body,
        content_type="application/json",
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="wrong",
    )
    assert denied.status_code == 401 and not TelegramInboundUpdate.objects.exists()
    accepted = client.post(
        "/api/v1/telegram/webhook",
        data=body,
        content_type="application/json",
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=settings.TELEGRAM_WEBHOOK_SECRET,
    )
    assert accepted.status_code == 200
    inbound = TelegramInboundUpdate.objects.get()
    assert inbound.webhook_authenticated_at is not None
    process_inbound_update(inbound.pk)
    assert BotAdminSession.objects.count() == 1
