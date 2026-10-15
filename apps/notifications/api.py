import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.notifications.models import Notification
from apps.notifications.services import mark_read
from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["notifications"], auth=mini_app_auth)


class NotificationOut(Schema):
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


@router.get("", response=NotificationPage)
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
        items=[
            NotificationOut(
                id=n.public_id,
                type=n.event_key,
                title=n.title,
                body=n.body,
                payload=n.payload,
                created_at=n.created_at,
                read_at=n.read_at,
                unread=n.read_at is None,
            )
            for n in rows[:limit]
        ],
        next_cursor=rows[limit - 1].pk if len(rows) > limit else None,
    )


@router.post("/{notification_id}/read", response=NotificationOut)
def read_notification(request: HttpRequest, notification_id: uuid.UUID) -> NotificationOut:
    player = getattr(request, "auth", None)
    assert isinstance(player, Player)
    try:
        n = mark_read(recipient=player, public_id=notification_id)
    except Notification.DoesNotExist as exc:
        raise HttpError(404, "Notification not found") from exc
    return NotificationOut(
        id=n.public_id,
        type=n.event_key,
        title=n.title,
        body=n.body,
        payload=n.payload,
        created_at=n.created_at,
        read_at=n.read_at,
        unread=False,
    )
