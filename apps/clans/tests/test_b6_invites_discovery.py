# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.clans.models import ClanJoinRequest, ClanMembership, ClanRole
from apps.clans.services import (
    ClanConflict,
    _fresh_authority,
    cancel_request,
    create_personal_invite,
    decide_request,
    eligible_groups,
    get_or_create_rejoin_invite,
    reconcile_telegram_membership,
    register_clan,
    request_join,
    revoke_invite_remote,
)
from apps.clans.tests.test_b5_integration import b5_http as b5_http
from apps.network.models import CreatorGroupRegistration, CreatorRegistrationIntent
from apps.notifications.models import Notification, TelegramDelivery
from apps.notifications.services import deliver_one_notification
from apps.players import group_avatars
from apps.players.models import GroupAvatar
from apps.telegram.bot_api import TelegramResult
from apps.telegram.models import TelegramGroup, TelegramGroupCandidate
from apps.telegram.services import process_inbound_update
from apps.telegram.tests.test_processing import inbound

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("purpose", ["JOIN", "REJOIN"])
def test_invites_remain_private_in_app_with_retry_and_cleanup(
    clan_world, b5_http, monkeypatch, purpose
):
    now, config, season, resolved, player, clan = clan_world
    cs, p = clan(), player(ClanRole.SOLDIER)
    p.first_private_contact_at = now
    p.save()
    monkeypatch.setattr("apps.clans.services._dispatch_revoke", lambda *_: None)
    created = []

    def create(*_):
        created.append(1)
        return TelegramResult(True, {"invite_link": "https://t.me/+B6_PRIVATE"})

    monkeypatch.setattr("apps.clans.services.bot_api.create_chat_invite_link", create)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.revoke_chat_invite_link", lambda *_: TelegramResult(True, {})
    )
    if purpose == "JOIN":
        req = request_join(p, cs, resolved)
        decide_request(cs.captain, req.pk, accept=True)
        invite = create_personal_invite(req.invite)
        assert not ClanMembership.objects.filter(player=p).exists()
    else:
        member = ClanMembership.objects.create(
            player=p,
            season=season,
            clan_season=cs,
            role="SOLDIER",
            access_state="ACCESS_LOST",
            started_at=now,
            access_rejoin_deadline=now + timedelta(hours=1),
            config_version=config,
        )
        invite = get_or_create_rejoin_invite(p, member)
    assert invite.state == "ACTIVE" and create_personal_invite(invite).pk == invite.pk
    assert len(created) == 1 and not TelegramDelivery.objects.exists()
    response = b5_http(p).get("/api/v1/clans/invite/current")
    assert (
        response.status_code == 200 and response.json()["invite_link"] == "https://t.me/+B6_PRIVATE"
    )
    outsider = b5_http(player(ClanRole.SOLDIER)).get("/api/v1/clans/invite/current")
    assert outsider.status_code == 404 and "B6_PRIVATE" not in outsider.content.decode()
    if purpose == "JOIN":
        cancel_request(p, req.pk)
        revoke_invite_remote(invite.pk)
        req.refresh_from_db()
        assert req.state == ClanJoinRequest.State.CANCELLED
    else:
        reconcile_telegram_membership(
            player=p,
            group=cs.clan.telegram_group,
            present=True,
            telegram_update_id=900,
            observed_at=timezone.now(),
        )
        member.refresh_from_db()
        assert member.access_state == "ACTIVE"
        revoke_invite_remote(invite.pk)
    invite.refresh_from_db()
    assert not invite.invite_link and not invite.unknown_credential_risk
    assert not TelegramDelivery.objects.exists()


def test_legacy_queued_invite_delivery_is_blocked_before_remote_send(clan_world, monkeypatch):
    _, _, _, _, player, _ = clan_world
    p = player()
    n = Notification.objects.create(
        recipient=p,
        event_key="CLAN_INVITE_READY",
        source_domain="CLAN",
        source_type="CLAN_INVITE",
        source_id="old",
        idempotency_key="legacy-invite",
    )
    row = TelegramDelivery.objects.create(
        notification=n,
        telegram_user_id=p.telegram_user_id,
        text_snapshot="https://t.me/+OLD_PRIVATE",
    )
    monkeypatch.setattr(
        "apps.notifications.services.bot_api.send_message",
        lambda *_: pytest.fail("Credential must not be sent"),
    )
    assert deliver_one_notification(row.pk) == 1
    row.refresh_from_db()
    assert row.state == "SKIPPED" and row.error_category == "invite_miniapp_only"


def candidate(clan_world):
    now, _, season, resolved, player, _ = clan_world
    captain = player(ClanRole.CAPTAIN)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-99999,
        chat_type="supergroup",
        title="Candidate",
        bot_is_admin=True,
        bot_can_invite_users=True,
        first_seen_at=now,
        last_seen_at=now,
    )
    TelegramGroupCandidate.objects.create(
        group=group, player=captain, telegram_user_id=captain.telegram_user_id, observed_at=now
    )
    return captain, group, season, resolved


@pytest.mark.parametrize("state", ["REVIEW", "ACTIVE", "ADMIN_REQUIRED", "SUSPENDED"])
def test_network_context_excluded_from_list_verification_and_registration(
    clan_world, state, monkeypatch
):
    captain, group, season, resolved = candidate(clan_world)
    CreatorGroupRegistration.objects.create(group=group, creator=captain, state=state)
    monkeypatch.setattr(
        "apps.clans.services.bot_api.get_chat_member",
        lambda *_: pytest.fail("Ineligible group must fail before HTTP"),
    )
    assert not eligible_groups(captain, season, resolved).exists()
    with pytest.raises(ClanConflict):
        _fresh_authority(captain, group)
    with pytest.raises(PermissionDenied):
        register_clan(
            captain, season, resolved, group_id=group.pk, name="New Clan", tag="NC", motto=""
        )


def test_final_registration_rechecks_network_context_created_during_remote_verification(
    clan_world, monkeypatch
):
    captain, group, season, resolved = candidate(clan_world)

    def verify(*_):
        CreatorGroupRegistration.objects.create(group=group, creator=captain, state="ACTIVE")
        return timezone.now(), 0

    monkeypatch.setattr("apps.clans.services._fresh_authority", verify)
    with pytest.raises(ClanConflict):
        register_clan(
            captain, season, resolved, group_id=group.pk, name="New Clan", tag="NC", motto=""
        )


def test_group_candidate_images_and_clan_add_bot_do_not_create_network_context(
    clan_world, settings, monkeypatch, b5_http
):
    captain, group, _, _ = candidate(clan_world)
    monkeypatch.setattr(group_avatars, "configured", lambda: True)
    settings.AVATAR_PUBLIC_API_ORIGIN = "https://api.example.com"
    assert b5_http(captain).get("/api/v1/clans/eligible-groups").json()[0]["image_url"] is None
    GroupAvatar.objects.filter(group=group).update(
        object_key="group-images/PRIVATE.jpg", expires_at=timezone.now() + timedelta(hours=1)
    )
    value = b5_http(captain).get("/api/v1/clans/eligible-groups").json()[0]["image_url"]
    assert value.startswith("https://api.example.com/api/v1/profile/group-avatar/")
    assert "PRIVATE" not in value and "telegram" not in value
    settings.TELEGRAM_BOT_ID = 500
    update = inbound(
        4567,
        {
            "my_chat_member": {
                "date": int(timezone.now().timestamp()),
                "chat": {"id": group.telegram_chat_id, "type": "supergroup", "title": "Clan route"},
                "from": {"id": captain.telegram_user_id},
                "new_chat_member": {
                    "status": "administrator",
                    "can_invite_users": True,
                    "user": {"id": 500},
                },
            }
        },
    )
    process_inbound_update(update.pk)
    assert not CreatorRegistrationIntent.objects.exists()
    assert not CreatorGroupRegistration.objects.exists()
