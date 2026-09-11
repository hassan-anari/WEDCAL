from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.players.models import Player
from apps.telegram.models import TelegramGroup, TelegramInboundUpdate, TelegramUserMembership
from apps.telegram.services import (
    process_inbound_update,
    purge_expired_ingress_payloads,
    set_group_monitoring,
)

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


@pytest.mark.django_db
def test_older_bot_membership_update_cannot_regress_newer_state(settings) -> None:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_BOT_ID = 500

    def change(update_id: int, status: str, title: str) -> TelegramInboundUpdate:
        return inbound(
            update_id,
            {
                "my_chat_member": {
                    "date": NOW + update_id,
                    "chat": {"id": -30, "type": "supergroup", "title": title},
                    "new_chat_member": {"status": status, "user": {"id": 500}},
                }
            },
        )

    process_inbound_update(change(200, "administrator", "New title").pk)
    process_inbound_update(change(100, "kicked", "Old title").pk)

    group = TelegramGroup.objects.get(telegram_chat_id=-30)
    assert group.bot_membership_status == "administrator"
    assert group.bot_is_admin is True
    assert group.bot_membership_last_update_id == 200
    assert group.title == "New title"


@pytest.mark.django_db
def test_older_user_membership_update_cannot_regress_newer_state() -> None:
    def change(update_id: int, status: str) -> TelegramInboundUpdate:
        return inbound(
            update_id,
            {
                "chat_member": {
                    "date": NOW + update_id,
                    "chat": {"id": -40, "type": "supergroup", "title": "Membership"},
                    "new_chat_member": {"status": status, "user": {"id": 900}},
                }
            },
        )

    process_inbound_update(change(300, "creator").pk)
    process_inbound_update(change(250, "left").pk)

    membership = TelegramUserMembership.objects.get(telegram_user_id=900)
    assert membership.status == "creator"
    assert membership.is_admin is True
    assert membership.is_owner is True
    assert membership.last_update_id == 300


@pytest.mark.django_db
def test_older_message_cannot_replace_newer_player_or_group_metadata() -> None:
    group = TelegramGroup.objects.create(
        telegram_chat_id=-45,
        chat_type="supergroup",
        is_monitored=True,
        first_seen_at=timezone.now(),
        last_seen_at=timezone.now(),
    )

    def observation(update_id: int, first_name: str, title: str) -> TelegramInboundUpdate:
        return inbound(
            update_id,
            {
                "message": {
                    "message_id": update_id,
                    "date": NOW + update_id,
                    "chat": {"id": -45, "type": "supergroup", "title": title},
                    "from": {"id": 902, "first_name": first_name},
                    "text": "message",
                }
            },
        )

    process_inbound_update(observation(350, "New name", "New group title").pk)
    process_inbound_update(observation(340, "Old name", "Old group title").pk)

    group.refresh_from_db()
    player = Player.objects.get(telegram_user_id=902)
    assert player.first_name == "New name"
    assert player.profile_last_telegram_update_id == 350
    assert group.title == "New group title"
    assert group.metadata_last_update_id == 350


@pytest.mark.django_db(transaction=True)
def test_existing_membership_links_when_player_is_later_created() -> None:
    membership_update = inbound(
        400,
        {
            "chat_member": {
                "date": NOW,
                "chat": {"id": -50, "type": "supergroup", "title": "Reconcile"},
                "new_chat_member": {"status": "member", "user": {"id": 901}},
            }
        },
    )
    process_inbound_update(membership_update.pk)
    membership = TelegramUserMembership.objects.get(telegram_user_id=901)
    assert membership.player is None

    start = inbound(
        401,
        {
            "message": {
                "message_id": 1,
                "date": NOW + 1,
                "chat": {"id": 901, "type": "private"},
                "from": {"id": 901, "first_name": "Linked"},
                "text": "/start",
            }
        },
    )
    process_inbound_update(start.pk)

    membership.refresh_from_db()
    player = Player.objects.get(telegram_user_id=901)
    assert membership.player == player
    assert Player.objects.filter(telegram_user_id=901).count() == 1


@pytest.mark.django_db
def test_retention_purges_only_expired_terminal_payloads() -> None:
    expired_at = timezone.now() - timedelta(seconds=1)
    processed = inbound(500, {"poll": {"id": "processed"}})
    ignored = inbound(501, {"poll": {"id": "ignored"}})
    pending = inbound(502, {"poll": {"id": "pending"}})
    failed = inbound(503, {"poll": {"id": "failed"}})
    TelegramInboundUpdate.objects.filter(
        pk__in=(processed.pk, ignored.pk, pending.pk, failed.pk)
    ).update(payload_expires_at=expired_at)
    TelegramInboundUpdate.objects.filter(pk=processed.pk).update(
        state=TelegramInboundUpdate.State.PROCESSED
    )
    TelegramInboundUpdate.objects.filter(pk=ignored.pk).update(
        state=TelegramInboundUpdate.State.IGNORED
    )
    TelegramInboundUpdate.objects.filter(pk=failed.pk).update(
        state=TelegramInboundUpdate.State.FAILED
    )

    assert purge_expired_ingress_payloads() == 2
    for update in (processed, ignored, pending, failed):
        update.refresh_from_db()
    assert processed.payload == {}
    assert ignored.payload == {}
    assert pending.payload != {}
    assert failed.payload != {}


@pytest.mark.django_db
def test_redrive_reclaims_stale_pending_and_failed(monkeypatch, settings) -> None:  # type: ignore[no-untyped-def]
    from apps.telegram.services import redrive_retryable_inbound_updates

    settings.TELEGRAM_INGRESS_PROCESSING_LEASE_SECONDS = 10
    settings.TELEGRAM_INGRESS_REDRIVE_BATCH_SIZE = 10
    old = timezone.now() - timedelta(minutes=1)
    pending = inbound(600, {"poll": {"id": "pending"}})
    failed = inbound(601, {"poll": {"id": "failed"}})
    TelegramInboundUpdate.objects.filter(pk=pending.pk).update(received_at=old)
    TelegramInboundUpdate.objects.filter(pk=failed.pk).update(
        state=TelegramInboundUpdate.State.FAILED, next_retry_at=old
    )
    dispatched = []
    monkeypatch.setattr(
        "apps.telegram.services.dispatch_inbound_update", lambda row: dispatched.append(row.pk)
    )
    assert redrive_retryable_inbound_updates() == 2
    assert set(dispatched) == {pending.pk, failed.pk}


@pytest.mark.django_db
def test_real_processing_poison_naturally_reaches_dead_letter(monkeypatch, settings) -> None:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_INGRESS_MAX_PROCESSING_ATTEMPTS = 2
    settings.TELEGRAM_INGRESS_RETRY_BASE_SECONDS = 0
    settings.TELEGRAM_INGRESS_RETRY_MAX_SECONDS = 0
    row = inbound(602, message_payload(-602))
    monkeypatch.setattr(
        "apps.telegram.services._apply_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("poison")),
    )
    with pytest.raises(RuntimeError):
        process_inbound_update(row.pk)
    row.refresh_from_db()
    assert row.processing_attempts == 1 and row.state == TelegramInboundUpdate.State.FAILED
    with pytest.raises(RuntimeError):
        process_inbound_update(row.pk)
    row.refresh_from_db()
    assert row.processing_attempts == 2
    assert row.state == TelegramInboundUpdate.State.DEAD_LETTER
    assert row.next_retry_at is None


@pytest.mark.django_db
def test_redrive_reclaims_worker_loss_without_starving_later_row(monkeypatch, settings) -> None:  # type: ignore[no-untyped-def]
    from apps.telegram.services import redrive_retryable_inbound_updates

    settings.TELEGRAM_INGRESS_PROCESSING_LEASE_SECONDS = 10
    settings.TELEGRAM_INGRESS_REDRIVE_BATCH_SIZE = 10
    old = timezone.now() - timedelta(minutes=1)
    abandoned = inbound(603, {"poll": {"id": "abandoned"}})
    later = inbound(604, {"poll": {"id": "later"}})
    TelegramInboundUpdate.objects.filter(pk=abandoned.pk).update(
        state=TelegramInboundUpdate.State.PROCESSING, claimed_at=old
    )
    TelegramInboundUpdate.objects.filter(pk=later.pk).update(received_at=old)
    dispatched = []
    monkeypatch.setattr(
        "apps.telegram.services.dispatch_inbound_update", lambda row: dispatched.append(row.pk)
    )
    assert redrive_retryable_inbound_updates() == 2
    assert set(dispatched) == {abandoned.pk, later.pk}
    assert (
        TelegramInboundUpdate.objects.filter(
            pk__in=dispatched, state=TelegramInboundUpdate.State.PENDING
        ).count()
        == 2
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_redrive_workers_claim_each_row_once(monkeypatch, settings) -> None:  # type: ignore[no-untyped-def]
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    from django.db import close_old_connections

    from apps.telegram.services import redrive_retryable_inbound_updates

    settings.TELEGRAM_INGRESS_PROCESSING_LEASE_SECONDS = 10
    old = timezone.now() - timedelta(minutes=1)
    rows = [inbound(700 + index, {"poll": {"id": str(index)}}) for index in range(8)]
    TelegramInboundUpdate.objects.filter(pk__in=[x.pk for x in rows]).update(received_at=old)
    barrier, lock, dispatched = Barrier(2), Lock(), []

    def dispatch(row) -> None:  # type: ignore[no-untyped-def]
        with lock:
            dispatched.append(row.pk)

    monkeypatch.setattr("apps.telegram.services.dispatch_inbound_update", dispatch)

    def worker() -> int:
        close_old_connections()
        try:
            barrier.wait()
            return redrive_retryable_inbound_updates(limit=4)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=10) for future in [pool.submit(worker), pool.submit(worker)]
        ]
    assert sum(results) == 8
    assert len(dispatched) == len(set(dispatched)) == 8


@pytest.mark.django_db
def test_worker_loss_at_attempt_cap_dead_letters_without_redispatch(monkeypatch, settings) -> None:  # type: ignore[no-untyped-def]
    from apps.telegram.services import redrive_retryable_inbound_updates

    settings.TELEGRAM_INGRESS_MAX_PROCESSING_ATTEMPTS = 2
    settings.TELEGRAM_INGRESS_PROCESSING_LEASE_SECONDS = 1
    row = inbound(900, {"poll": {"id": "abandoned"}})
    TelegramInboundUpdate.objects.filter(pk=row.pk).update(
        state=TelegramInboundUpdate.State.PROCESSING,
        processing_attempts=2,
        claimed_at=timezone.now() - timedelta(minutes=1),
    )
    calls: list[int] = []
    monkeypatch.setattr(
        "apps.telegram.services.dispatch_inbound_update", lambda item: calls.append(item.pk)
    )
    assert redrive_retryable_inbound_updates() == 0
    row.refresh_from_db()
    assert row.state == TelegramInboundUpdate.State.DEAD_LETTER
    assert not calls


@pytest.mark.django_db
def test_retention_purges_dead_letter_but_keeps_retryable_payloads() -> None:
    expired = timezone.now() - timedelta(seconds=1)
    dead = inbound(901, {"poll": {"id": "dead"}})
    retryable = inbound(902, {"poll": {"id": "retry"}})
    TelegramInboundUpdate.objects.filter(pk=dead.pk).update(
        state=TelegramInboundUpdate.State.DEAD_LETTER, payload_expires_at=expired
    )
    TelegramInboundUpdate.objects.filter(pk=retryable.pk).update(
        state=TelegramInboundUpdate.State.FAILED, payload_expires_at=expired
    )
    assert purge_expired_ingress_payloads() == 1
    dead.refresh_from_db()
    retryable.refresh_from_db()
    assert dead.payload == {} and retryable.payload != {}
