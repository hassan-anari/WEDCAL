# ruff: noqa: F811
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.network.models import CreatorGroupRegistration
from apps.network.services import (
    consume_registration_token,
    create_registration_intent,
    verify_registration,
)
from apps.network.tasks import refresh_due_owned_groups
from apps.notifications.audiences import grant_registration_consent, set_group_consent
from apps.notifications.models import GroupBroadcastSubscription
from apps.notifications.services import deliver_broadcasts, materialize_campaign, queue_campaign
from apps.notifications.tests.test_b3_audiences import audience_world, campaign  # noqa: F401
from apps.telegram.bot_api import TelegramResult
from apps.telegram.models import TelegramGroup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def registration_world(phase4_config_factory, player_factory, settings, monkeypatch):
    now = timezone.now()
    phase4_config_factory(effective_at=now - timedelta(days=1))
    settings.TELEGRAM_BOT_ID = 999
    settings.TELEGRAM_BOT_USERNAME = "SyntheticB4Bot"
    creator = player_factory()
    group = TelegramGroup.objects.create(
        telegram_chat_id=-1001,
        chat_type="supergroup",
        title="New intentional registration",
        first_seen_at=now,
        last_seen_at=now,
    )

    def membership(chat, user):
        return TelegramResult(True, {"status": "administrator", "user": {"id": user}})

    monkeypatch.setattr("apps.network.services.get_chat_member", membership)
    monkeypatch.setattr("apps.notifications.audiences.bot_api.get_chat_member", membership)
    monkeypatch.setattr("apps.network.tasks.dispatch_verification", lambda *_: None)
    return creator, group


def test_registration_requires_creator_bound_intent_and_verified_explicit_consent(
    registration_world, player_factory
):
    creator, group = registration_world
    intent, link = create_registration_intent(creator, authorize_group_messages=True)
    token = link.split("startgroup=", 1)[1]
    assert consume_registration_token(token, player_factory(101), group) is None
    registration = consume_registration_token(token, creator, group)
    assert registration and registration.authorization_intent_id == intent.pk
    assert not GroupBroadcastSubscription.objects.exists()
    assert verify_registration(registration.pk) == "ACTIVE"
    consent = GroupBroadcastSubscription.objects.get()
    assert consent.enabled and consent.authorized_telegram_user_id == creator.telegram_user_id
    assert consume_registration_token(token, creator, group) is None
    set_group_consent(group, creator.telegram_user_id, enabled=False)
    assert verify_registration(registration.pk) == "ACTIVE"
    consent.refresh_from_db()
    assert not consent.enabled and consent.revoked_at


def test_historical_registration_and_default_intent_never_enroll(registration_world):
    creator, group = registration_world
    registration = CreatorGroupRegistration.objects.create(creator=creator, group=group)
    assert verify_registration(registration.pk) == "ACTIVE"
    assert not GroupBroadcastSubscription.objects.exists()
    intent, link = create_registration_intent(creator)
    assert not intent.authorize_group_messages
    consume_registration_token(link.split("startgroup=", 1)[1], creator, group)
    registration.refresh_from_db()
    assert registration.authorization_intent_id is None
    grant_registration_consent(registration.pk)
    assert not GroupBroadcastSubscription.objects.exists()


def test_wrong_telegram_identity_and_authority_loss_fail_closed(registration_world, monkeypatch):
    creator, group = registration_world
    registration = CreatorGroupRegistration.objects.create(creator=creator, group=group)
    monkeypatch.setattr(
        "apps.network.services.get_chat_member",
        lambda chat, user: TelegramResult(
            True, {"status": "administrator", "user": {"id": user + 1}}
        ),
    )
    assert verify_registration(registration.pk) == "ADMIN_REQUIRED"
    assert not GroupBroadcastSubscription.objects.exists()


def test_polling_owned_groups_coalesces_verification_dispatch(registration_world, monkeypatch):
    creator, group = registration_world
    row = CreatorGroupRegistration.objects.create(creator=creator, group=group)
    dispatched: list[int] = []
    monkeypatch.setattr("apps.network.tasks.dispatch_verification", dispatched.append)
    refresh_due_owned_groups(creator.pk)
    refresh_due_owned_groups(creator.pk)
    assert dispatched == [row.pk]


def test_creator_authority_loss_prevents_later_broadcast_send(audience_world, monkeypatch):  # noqa: F811
    actor, creator, group = audience_world
    set_group_consent(group, creator.telegram_user_id, enabled=True)
    row = campaign(actor, "GROUPS")
    queue_campaign(campaign=row, actor=actor, reason="Explicit authorized confirmation")
    materialize_campaign(row.pk)

    def lost(chat, user):
        return TelegramResult(
            True,
            {
                "status": "member" if user == creator.telegram_user_id else "administrator",
                "user": {"id": user},
            },
        )

    monkeypatch.setattr("apps.notifications.audiences.bot_api.get_chat_member", lost)
    calls = []
    monkeypatch.setattr("apps.telegram.bot_api.send_message", lambda *args: calls.append(args))
    deliver_broadcasts()
    target = row.recipients.get()
    assert target.state == "SKIPPED" and not calls


@pytest.mark.parametrize(
    "interleaving", ["registration_inactive", "bot_removed", "consent_revoked"]
)
def test_registration_consent_rechecks_state_after_external_verification(
    registration_world, monkeypatch, interleaving
):
    creator, group = registration_world
    intent, link = create_registration_intent(creator, authorize_group_messages=True)
    registration = consume_registration_token(link.split("startgroup=", 1)[1], creator, group)
    assert registration
    CreatorGroupRegistration.objects.filter(pk=registration.pk).update(state="ACTIVE")

    def verify(*args):
        # Deterministically interleave a committed authority/revocation change during network I/O.
        if interleaving == "registration_inactive":
            CreatorGroupRegistration.objects.filter(pk=registration.pk).update(state="INACTIVE")
        elif interleaving == "bot_removed":
            TelegramGroup.objects.filter(pk=group.pk).update(
                bot_is_admin=False,
                bot_membership_status="left",
                bot_membership_last_update_id=123,
            )
        else:
            GroupBroadcastSubscription.objects.create(
                group=group,
                enabled=False,
                revision=1,
                authorized_telegram_user_id=creator.telegram_user_id,
                revoked_at=timezone.now(),
            )
        return True

    monkeypatch.setattr("apps.notifications.audiences.verify_group", verify)
    grant_registration_consent(registration.pk)
    assert not GroupBroadcastSubscription.objects.filter(enabled=True).exists()
