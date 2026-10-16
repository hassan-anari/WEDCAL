"""Queue versioned Welcome content without interpolation or automatic re-enrollment."""

from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import BroadcastSource, Notification, TelegramDelivery
from apps.players.models import Player


@transaction.atomic
def queue_private_message(
    player: Player,
    *,
    key: str,
    text: str,
    options: dict[str, object] | None = None,
    received_at: datetime | None = None,
    copy_source: BroadcastSource | None = None,
) -> TelegramDelivery:
    notification, _ = Notification.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "recipient": player,
            "event_key": "BOT_PRIVATE",
            "source_domain": "telegram",
            "source_type": "PRIVATE_CONTROL",
            "source_id": key,
            "body": "",
            "read_at": timezone.now(),
        },
    )
    delivery, _ = TelegramDelivery.objects.get_or_create(
        notification=notification,
        defaults={
            "telegram_user_id": player.telegram_user_id,
            "text_snapshot": text,
            "source_received_at": received_at,
            "message_options": options or {},
            "copy_source": copy_source,
        },
    )
    return delivery


def queue_welcome(player: Player) -> None:
    if not (settings.B3_BOT_WELCOME_ENABLED and settings.B4_PUBLIC_WELCOME_ENABLED):
        return
    from apps.notifications.models import WelcomeConfiguration

    config = WelcomeConfiguration.objects.select_related("active_template").filter(pk=1).first()
    if not config or not config.active_template:
        return
    template = config.active_template
    template.full_clean()
    queue_private_message(
        player,
        key=f"bot-welcome:{player.pk}",
        text=template.body,
        options={
            "parse_mode": "HTML",
            "reply_markup": template.keyboard,
            "link_preview_options": {"is_disabled": True},
        },
    )
