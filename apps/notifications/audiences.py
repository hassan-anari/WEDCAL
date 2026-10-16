from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import GroupBroadcastConsentEvent, GroupBroadcastSubscription
from apps.players.models import Player
from apps.telegram import bot_api
from apps.telegram.models import TelegramGroup


def eligible_users() -> Any:
    return Player.objects.filter(first_private_contact_at__isnull=False)


def eligible_groups() -> Any:
    # Known membership observations only; Telegram cannot enumerate unknown groups.
    return TelegramGroup.objects.filter(
        chat_type__in=["group", "supergroup"],
        telegram_chat_id__lt=0,
        bot_membership_status__in=["administrator", "creator"],
        bot_is_admin=True,
    )


def verify_broadcast_group(group: TelegramGroup) -> bool:
    """Bot admin authority, independent from Creator ownership and legacy preferences."""
    if not eligible_groups().filter(pk=group.pk).exists() or not settings.TELEGRAM_BOT_ID:
        return False
    result = bot_api.get_chat_member(group.telegram_chat_id, settings.TELEGRAM_BOT_ID)
    if not result.ok:
        if result.category in {"forbidden", "chat_not_found"}:
            return False
        raise ValidationError("Telegram verification temporarily unavailable")
    member = result.result or {}
    if (
        (member.get("user") or {}).get("id") != settings.TELEGRAM_BOT_ID
        or member.get("status") not in {"administrator", "creator"}
        or member.get("can_send_messages") is False
        or member.get("is_member") is False
    ):
        return False
    # A newer committed membership update may have observed loss during network I/O.
    return bool(eligible_groups().filter(pk=group.pk).exists())


def verify_group(group: TelegramGroup, user_id: int | None = None) -> bool:
    if (
        group.telegram_chat_id >= 0
        or group.chat_type not in {"group", "supergroup"}
        or not settings.TELEGRAM_BOT_ID
    ):
        return False
    bot = bot_api.get_chat_member(group.telegram_chat_id, settings.TELEGRAM_BOT_ID)
    if not bot.ok:
        raise ValidationError("Telegram verification temporarily unavailable")
    member = bot.result or {}
    if (member.get("user") or {}).get("id") != settings.TELEGRAM_BOT_ID:
        return False
    status = member.get("status")
    allowed = status in {"creator", "administrator"}
    if status in {"member", "restricted"}:
        chat = bot_api.request("getChat", {"chat_id": group.telegram_chat_id})
        if not chat.ok:
            raise ValidationError("Telegram permissions temporarily unavailable")
        allowed = ((chat.result or {}).get("permissions") or {}).get("can_send_messages") is True
        if status == "restricted":
            allowed = (
                allowed
                and member.get("is_member") is True
                and member.get("can_send_messages") is True
            )
    if not allowed:
        return False
    if user_id is not None:
        actor = bot_api.get_chat_member(group.telegram_chat_id, user_id)
        if not actor.ok:
            raise ValidationError("Telegram administrator verification temporarily unavailable")
        detail = actor.result or {}
        if (detail.get("user") or {}).get("id") != user_id or detail.get("status") not in {
            "creator",
            "administrator",
        }:
            return False
    return True


@transaction.atomic
def set_group_consent(
    group: TelegramGroup, telegram_user_id: int, *, enabled: bool
) -> GroupBroadcastSubscription:
    TelegramGroup.objects.select_for_update().get(pk=group.pk)
    row = GroupBroadcastSubscription.objects.select_for_update().filter(group=group).first()
    if not enabled and row and row.authorized_telegram_user_id == telegram_user_id:
        pass  # The consenting actor can always revoke their own consent.
    elif not verify_group(group, telegram_user_id):
        raise ValidationError("Group administrator and bot send permission are required")
    if row is None:
        row = GroupBroadcastSubscription(group=group, authorized_telegram_user_id=telegram_user_id)
    row.enabled = enabled
    row.revision += 1
    row.authorized_telegram_user_id = telegram_user_id
    row.consented_at = timezone.now() if enabled else row.consented_at
    row.verified_at = timezone.now()
    row.revoked_at = None if enabled else timezone.now()
    row.save()
    GroupBroadcastConsentEvent.objects.create(
        subscription=row,
        actor_telegram_user_id=telegram_user_id,
        enabled=enabled,
        revision=row.revision,
    )
    return row


def estimated_targets(audience: str) -> dict[str, int]:
    return {
        "users": eligible_users().count()
        if audience in {"USERS", "BOTH", "ALL", "TELEGRAM_REACHABLE"}
        else 0,
        "groups": eligible_groups().count() if audience in {"GROUPS", "BOTH"} else 0,
    }
