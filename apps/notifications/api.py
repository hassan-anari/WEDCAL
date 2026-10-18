import uuid
from datetime import datetime
from typing import Literal

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from apps.core.b3 import B3Error, b3_responses
from apps.notifications.clan_copy import display_copy
from apps.notifications.models import Notification
from apps.notifications.services import mark_read
from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["notifications"], auth=mini_app_auth)


class NotificationLink(Schema):
    path: Literal["/clan", "/profile", "/earn", "/arena"]


def deep_link(notification: Notification) -> NotificationLink | None:
    # Only canonical domain event types select routes. Payload never supplies a URL.
    if notification.source_domain.casefold() in {"clan", "clans"}:
        return NotificationLink(path="/clan")
    if notification.source_domain.casefold() == "profile_collection":
        return NotificationLink(path="/profile")
    return None


class NotificationOut(Schema):
    deep_link: NotificationLink | None = None
    id: uuid.UUID
    type: str
    title: str
    body: str
    payload: dict[str, object]
    created_at: datetime
    read_at: datetime | None
    unread: bool


class NotificationPage(Schema):
    items: list[NotificationOut]
    next_cursor: int | None
    unread_count: int


@router.get("", response=b3_responses(NotificationPage))
def list_notifications(
    request: HttpRequest, cursor: int | None = None, limit: int = 20
) -> NotificationPage:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    limit = max(1, min(limit, 100))
    query = (
        Notification.objects.filter(recipient=player)
        .exclude(event_key="BOT_PRIVATE")
        .order_by("-id")
    )
    if cursor is not None:
        query = query.filter(id__lt=cursor)
    rows = list(query[: limit + 1])
    return NotificationPage(
        items=[notification_out(n) for n in rows[:limit]],
        next_cursor=rows[limit - 1].pk if len(rows) > limit else None,
        unread_count=Notification.objects.filter(recipient=player, read_at__isnull=True)
        .exclude(event_key="BOT_PRIVATE")
        .count(),
    )


class ReadAllOutput(Schema):
    updated_count: int
    unread_count: int


@router.post("/read-all", response=b3_responses(ReadAllOutput))
def read_all(request: HttpRequest) -> ReadAllOutput:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    updated = (
        Notification.objects.filter(recipient=player, read_at__isnull=True)
        .exclude(event_key="BOT_PRIVATE")
        .update(read_at=timezone.now())
    )
    unread = (
        Notification.objects.filter(recipient=player, read_at__isnull=True)
        .exclude(event_key="BOT_PRIVATE")
        .count()
    )
    return ReadAllOutput(updated_count=updated, unread_count=unread)


@router.post("/{notification_id}/read", response=b3_responses(NotificationOut))
def read_notification(request: HttpRequest, notification_id: uuid.UUID) -> NotificationOut:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    try:
        n = mark_read(recipient=player, public_id=notification_id)
    except Notification.DoesNotExist as exc:
        raise B3Error("NOTIFICATION_NOT_FOUND", 404) from exc
    return notification_out(n)


def notification_out(n: Notification) -> NotificationOut:
    title, body = display_copy(n)
    return NotificationOut(
        deep_link=deep_link(n),
        id=n.public_id,
        type=n.event_key,
        title=title,
        body=body,
        payload=n.payload,
        created_at=n.created_at,
        read_at=n.read_at,
        unread=n.read_at is None,
    )
