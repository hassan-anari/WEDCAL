"""Group cache shares sanitization, Telegram budgets and refresh slots with player avatars."""

from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone

from apps.players.avatar_budget import avatar_refresh_slot
from apps.players.avatar_storage import S3GroupAvatarStorage
from apps.players.avatars import (
    AvatarRetry,
    AvatarSourceError,
    bot_call,
    error_category,
    retrieve_file,
)
from apps.players.models import GroupAvatar
from apps.telegram.models import TelegramGroup


def configured() -> bool:
    origin = urlsplit(settings.AVATAR_PUBLIC_API_ORIGIN)
    endpoint = urlsplit(settings.GROUP_IMAGE_S3_ENDPOINT_URL)
    return bool(
        settings.GROUP_IMAGE_CACHE_ENABLED
        and 1 <= settings.AVATAR_CACHE_MAX_CONCURRENT_REFRESHES <= 16
        and 1 <= settings.AVATAR_CACHE_TTL_SECONDS <= 2592000
        and 1 <= settings.AVATAR_CACHE_MAX_BYTES <= 5242880
        and 1 <= settings.AVATAR_CACHE_MAX_PIXELS <= 16000000
        and settings.GROUP_IMAGE_S3_BUCKET
        and settings.GROUP_IMAGE_S3_ACCESS_KEY_ID
        and settings.GROUP_IMAGE_S3_SECRET_ACCESS_KEY
        and endpoint.scheme == "https"
        and endpoint.hostname
        and not endpoint.username
        and not endpoint.query
        and not endpoint.fragment
        and origin.scheme == "https"
        and origin.hostname
        and not origin.username
        and not origin.query
        and not origin.fragment
        and origin.path in ("", "/")
    )


def group_avatar_url(group: TelegramGroup) -> str:
    if not configured():
        return ""
    row, _ = GroupAvatar.objects.get_or_create(
        group=group, defaults={"refresh_after": timezone.now()}
    )
    if row.object_key and row.expires_at and row.expires_at > timezone.now():
        origin = settings.AVATAR_PUBLIC_API_ORIGIN.rstrip("/")
        return f"{origin}/api/v1/profile/group-avatar/{row.delivery_id}"
    return ""


def refresh_group_avatar(group_id: int) -> None:
    if not configured():
        return
    from apps.clans.models import ClanSeason
    from apps.network.models import CreatorGroupRegistration

    if not (
        CreatorGroupRegistration.objects.filter(group_id=group_id).exists()
        or ClanSeason.objects.filter(
            clan__telegram_group_id=group_id, state__in=["ACTIVE", "RECOVERY"]
        ).exists()
        or TelegramGroup.objects.filter(pk=group_id, bot_is_admin=True, bot_can_invite_users=True)
        .filter(Q(memberships__is_admin=True) | Q(candidates__isnull=False))
        .exists()
    ):
        return
    now, token = timezone.now(), uuid4()
    with avatar_refresh_slot() as available:
        if not available:
            GroupAvatar.objects.filter(group_id=group_id, refresh_after__lte=now).update(
                refresh_after=now + timedelta(seconds=settings.AVATAR_CACHE_RETRY_SECONDS)
            )
            return
        with transaction.atomic():
            row = GroupAvatar.objects.select_for_update().get(group_id=group_id)
            if row.refresh_after > now:
                return
            row.refresh_token = token
            row.refresh_after = now + timedelta(seconds=settings.AVATAR_CACHE_RETRY_SECONDS)
            row.save()
            group = TelegramGroup.objects.get(pk=group_id)
        key = ""
        try:
            storage = S3GroupAvatarStorage()
            chat = bot_call("getChat", {"chat_id": group.telegram_chat_id})
            if chat.get("id") != group.telegram_chat_id:
                raise AvatarSourceError("group_identity")
            photo = chat.get("photo") or {}
            file_id = photo.get("big_file_id")
            if file_id:
                content = retrieve_file(file_id)
                key = f"group-images/{uuid4()}.jpg"
                storage.put(key, content)
            with transaction.atomic():
                row = GroupAvatar.objects.select_for_update().get(group_id=group_id)
                if row.refresh_token != token:
                    storage.delete(key)
                    return
                old_key = row.object_key
                row.object_key, row.delivery_id = key, uuid4()
                row.expires_at = now + timedelta(seconds=settings.AVATAR_CACHE_TTL_SECONDS)
                row.refresh_after, row.refresh_token = row.expires_at, None
                row.last_error_category = ""
                row.save()
            storage.delete(old_key)
        except Exception as exc:
            delay = max(
                settings.AVATAR_CACHE_RETRY_SECONDS,
                exc.retry_after if isinstance(exc, AvatarRetry) else 0,
            )
            GroupAvatar.objects.filter(group_id=group_id, refresh_token=token).update(
                refresh_token=None,
                refresh_after=now + timedelta(seconds=delay),
                last_error_category=error_category(exc),
            )
            structlog.get_logger(__name__).warning(
                "group_avatar_refresh_failed", group_id=group_id, category=error_category(exc)
            )


def deliver_group_avatar(delivery_id: UUID) -> HttpResponse:
    if not configured():
        return HttpResponse(status=404)
    row = GroupAvatar.objects.filter(delivery_id=delivery_id, expires_at__gt=timezone.now()).first()
    if not row or not row.object_key:
        return HttpResponse(status=404)
    try:
        data = S3GroupAvatarStorage().get(row.object_key)
    except Exception:
        return HttpResponse(status=503, headers={"Retry-After": "60", "Cache-Control": "no-store"})
    return HttpResponse(
        data,
        content_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


def refresh_due_groups(limit: int = 20) -> int:
    if not configured():
        return 0
    ids = list(
        GroupAvatar.objects.filter(refresh_after__lte=timezone.now())
        .order_by("refresh_after")
        .values_list("group_id", flat=True)[:limit]
    )
    for group_id in ids:
        refresh_group_avatar(group_id)
    return len(ids)


def purge_group_avatars(limit: int = 100) -> int:
    if not configured():
        return 0
    storage = S3GroupAvatarStorage()
    ids = list(
        GroupAvatar.objects.filter(expires_at__lte=timezone.now())
        .order_by("expires_at")
        .values_list("pk", flat=True)[:limit]
    )
    count = 0
    for pk in ids:
        with transaction.atomic():
            row = (
                GroupAvatar.objects.select_for_update()
                .filter(pk=pk, expires_at__lte=timezone.now())
                .first()
            )
            if not row:
                continue
            try:
                storage.delete(row.object_key)
            except Exception:
                continue
            row.object_key, row.expires_at, row.delivery_id = "", None, uuid4()
            row.save()
            count += 1
    return count
