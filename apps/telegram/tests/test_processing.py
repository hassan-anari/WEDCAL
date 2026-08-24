from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from django.utils import timezone

from apps.players.models import Player
from apps.telegram.models import TelegramGroup, TelegramInboundUpdate, TelegramUserMembership
from apps.telegram.services import process_inbound_update, set_group_monitoring

NOW = int(datetime.now(tz=UTC).timestamp())


def inbound(update_id: int, payload: Mapping[str, object]) -> TelegramInboundUpdate:
    return TelegramInboundUpdate.objects.create(
        update_id=update_id,
        payload={"update_id": update_id, **payload},
        payload_expires_at=timezone.now(),
    )


def message_payload(chat_id: int, *, chat_type: str = "supergroup", is_bot: bool = False):  # type: ignore[no-untyped-def]
    return {
        "message": {
            "message_id": 1,
            "date": NOW,
            "chat": {"id": chat_id, "type": chat_type, "title": "Test"},
            "from": {"id": 77, "first_name": "User", "is_bot": is_bot},
            "text": "hello",
        }
    }


@pytest.mark.django_db
def test_monitored_group_message_creates_player_but_unmonitored_does_not() -> None:
    process_inbound_update(inbound(1, message_payload(-1001)).pk)
    assert Player.objects.count() == 0
    group = TelegramGroup.objects.get(telegram_chat_id=-1001)
    set_group_monitoring(group, enabled=True)
    process_inbound_update(inbound(2, message_payload(-1001)).pk)
    assert Player.objects.get().first_seen_source == Player.FirstSeenSource.GROUP_ACTIVITY


@pytest.mark.django_db
def test_bot_sender_does_not_become_player() -> None:
    group = TelegramGroup.objects.create(
        telegram_chat_id=-2,
        chat_type="group",
        is_monitored=True,
        first_seen_at=timezone.now(),
        last_seen_at=timezone.now(),
    )
    assert group.is_monitored
    process_inbound_update(inbound(3, message_payload(-2, chat_type="group", is_bot=True)).pk)
    assert Player.objects.count() == 0


@pytest.mark.django_db
def test_private_start_preserves_first_parameter_and_is_retry_safe() -> None:
    def start(parameter: str) -> dict[str, object]:
        return {
            "message": {
                "message_id": 1,
                "date": NOW,
                "chat": {"id": 77, "type": "private"},
                "from": {"id": 77, "first_name": "User"},
                "text": f"/start {parameter}",
            }
        }

    first = inbound(4, start("original"))
    process_inbound_update(first.pk)
    process_inbound_update(first.pk)
    process_inbound_update(inbound(5, start("replacement")).pk)
    player = Player.objects.get()
    assert player.first_seen_source == Player.FirstSeenSource.BOT_START
    assert player.first_start_parameter == "original"
    assert player.first_private_contact_at is not None
    assert TelegramInboundUpdate.objects.get(pk=first.pk).processing_attempts == 1


@pytest.mark.django_db
def test_group_start_is_not_private_onboarding() -> None:
    process_inbound_update(inbound(6, message_payload(-6)).pk)
    assert Player.objects.count() == 0


@pytest.mark.django_db
def test_chat_member_observes_unknown_user_without_creating_player() -> None:
    payload = {
        "chat_member": {
            "date": NOW,
            "chat": {"id": -10, "type": "supergroup", "title": "Members"},
            "new_chat_member": {"status": "administrator", "user": {"id": 1234}},
        }
    }
    process_inbound_update(inbound(7, payload).pk)
    membership = TelegramUserMembership.objects.get()
    assert membership.player is None
    assert membership.is_admin is True
    assert Player.objects.count() == 0


@pytest.mark.django_db
def test_bot_membership_and_identity_mismatch(settings) -> None:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_BOT_ID = 500

    def membership(update_id: int, bot_id: int, status: str) -> TelegramInboundUpdate:
        return inbound(
            update_id,
            {
                "my_chat_member": {
                    "date": NOW,
                    "chat": {"id": -20, "type": "supergroup", "title": "Bot group"},
                    "new_chat_member": {"status": status, "user": {"id": bot_id}},
                }
            },
        )

    process_inbound_update(membership(8, 501, "administrator").pk)
    assert TelegramGroup.objects.count() == 0
    process_inbound_update(membership(9, 500, "administrator").pk)
    group = TelegramGroup.objects.get()
    assert group.bot_is_admin is True
    process_inbound_update(membership(10, 500, "kicked").pk)
    group.refresh_from_db()
    assert group.bot_is_admin is False
    assert group.bot_membership_status == "kicked"


@pytest.mark.django_db
def test_unknown_update_is_safely_ignored() -> None:
    update = inbound(11, {"poll": {"id": "unknown"}})
    process_inbound_update(update.pk)
    update.refresh_from_db()
    assert update.state == TelegramInboundUpdate.State.IGNORED
