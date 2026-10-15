# ruff: noqa: E501
"""Static approved copy. Telegram HTML; no user interpolation or Markdown escaping."""

from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import Notification, TelegramDelivery
from apps.players.models import Player

WELCOME = """🌌 <b>WELCOME TO DEXO LAND!</b> 🚀✨

Hey, Explorer! 👋

A whole new universe is waiting for you! 🌍💫

Welcome to <b>Dexo Land</b> — a world of adventure, evolution, challenges, and epic rewards!

Every great legend starts somewhere… and yours begins right here! ⚡

🥚 <b>MEET YOUR DEXO!</b>

Your journey begins with a little companion full of potential. Earn XP, level up, unlock new evolutions, and watch your Dexo grow from a tiny explorer into something LEGENDARY! 🌟

🎮 <b>YOUR ADVENTURE AWAITS!</b>

👆 Tap, interact, and power up your Dexo!

🎯 Complete missions and daily challenges.

⌨️ Put your skills to the test in the Typing Arena.

🤝 Invite friends and grow your network.

🏰 Join Clans, team up, and climb the rankings!

🪙 <b>6 WEEKS. 6 DLX ALLOCATION ROUNDS. BIG OPPORTUNITIES!</b> 🎁

Every week brings a fresh <b>DLX allocation round</b>, giving eligible Explorers another opportunity to shine!

🔥 Stay active. Complete challenges. Grow your community. Make every week count!

Your qualifying performance throughout the season can help you compete for a share of the weekly DLX rewards.

The journey doesn't stop after one week — every new round is another chance to make your mark!

💜 <b>ADVENTURES ARE BETTER TOGETHER!</b>

Join our official community, meet fellow Explorers, catch the latest announcements, and discover what's coming next!

The Dexo Land universe is just getting started… and YOU are part of its story!

🚀 <b>READY TO AWAKEN YOUR DEXO?</b>

Your adventure starts NOW!

<b>LET'S GO, EXPLORER!</b> 🎮🔥

DLX allocations depend on eligibility, are off-chain, and are not guaranteed earnings or an immediately withdrawable balance."""


def keyboard() -> dict[str, object]:
    url = settings.TELEGRAM_MINI_APP_URL
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Official Mini App URL must be HTTPS")
    return {
        "inline_keyboard": [
            [
                {"text": "🎮 Play Dexo Land", "web_app": {"url": url}},
                {"text": "💜 Join Community", "url": "https://t.me/Dalexio_Official"},
            ]
        ]
    }


@transaction.atomic
def queue_private_message(
    player: Player, *, key: str, text: str, options: dict[str, object] | None = None
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
            "message_options": options or {},
        },
    )
    return delivery


def queue_welcome(player: Player) -> None:
    if not settings.B3_BOT_WELCOME_ENABLED:
        return
    queue_private_message(
        player,
        key=f"bot-welcome:{player.pk}",
        text=WELCOME,
        options={
            "parse_mode": "HTML",
            "reply_markup": keyboard(),
            "link_preview_options": {"is_disabled": True},
        },
    )
