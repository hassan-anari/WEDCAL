"""Versioned static Telegram HTML with controlled recipients and activation."""

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.notifications.models import (
    BotAdminOperator,
    OperatorAuditEvent,
    WelcomeConfiguration,
    WelcomeTemplate,
)
from apps.players.models import Player


def safe_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValidationError("A full approved HTTPS URL is required")
    try:
        parts = urlsplit(value)
        decoded = unquote(value)
        if parts.query and (
            parts.hostname != "t.me"
            or set(parse_qs(parts.query)) - {"start", "startapp", "startgroup"}
        ):
            raise ValueError
        if (
            parts.scheme != "https"
            or parts.hostname not in settings.TELEGRAM_WELCOME_ALLOWED_HOSTS
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
            or parts.fragment
            or any(ord(c) < 33 or c == "\\" for c in decoded)
            or "%" in decoded
            or any(x in parts.path.split("/") for x in (".", ".."))
        ):
            raise ValueError
    except ValueError as exc:
        raise ValidationError(
            "Use an approved HTTPS host without credentials, fragments or unsafe escaping"
        ) from exc
    return value


class TelegramHTML(HTMLParser):
    allowed = {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "a",
        "tg-spoiler",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.allowed or (tag != "a" and attrs):
            raise ValidationError("Unsupported Telegram HTML tag or attribute")
        if tag == "a":
            if len(attrs) != 1 or attrs[0][0] != "href":
                raise ValidationError("Links require one safe href")
            safe_url(attrs[0][1])
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            raise ValidationError("Telegram HTML tags must be balanced")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raise ValidationError("Self-closing Telegram HTML is unsupported")

    def handle_entityref(self, name: str) -> None:
        if name not in {"lt", "gt", "amp", "quot"}:
            raise ValidationError("Unsupported Telegram HTML entity")

    def handle_charref(self, name: str) -> None:
        try:
            value = int(name[1:], 16) if name.startswith("x") else int(name)
            if value < 32 or value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                raise ValueError
        except ValueError as exc:
            raise ValidationError("Invalid HTML character reference") from exc

    def handle_comment(self, data: str) -> None:
        raise ValidationError("HTML comments are unsupported")

    def handle_decl(self, decl: str) -> None:
        raise ValidationError("HTML declarations are unsupported")


def validate_template(body: str, keyboard: Any) -> None:
    if (
        not body.strip()
        or len(body.encode("utf-16-le")) // 2 > 4096
        or any(ord(c) < 32 and c not in "\n\t" for c in body)
        or "{" in body
        or "}" in body
    ):
        raise ValidationError(
            "Welcome must be 1–4096 Telegram characters with no template interpolation"
        )
    parser = TelegramHTML()
    parser.feed(body)
    parser.close()
    if parser.stack or re.search(r"<(?!/?[a-z-]+(?:[ >]))", body):
        raise ValidationError("Malformed Telegram HTML")
    if not isinstance(keyboard, dict) or set(keyboard) != {"inline_keyboard"}:
        raise ValidationError("Use an inline_keyboard object")
    rows = keyboard["inline_keyboard"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValidationError("Use 1–8 keyboard rows")
    count = 0
    for row in rows:
        if not isinstance(row, list) or not 1 <= len(row) <= 8:
            raise ValidationError("Use 1–8 buttons per row")
        for button in row:
            count += 1
            if not isinstance(button, dict) or set(button) not in (
                {"text", "url"},
                {"text", "web_app"},
            ):
                raise ValidationError("Buttons require text and exactly one URL or Web App")
            text = button["text"]
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 64
                or any(ord(c) < 32 for c in text)
            ):
                raise ValidationError("Button text must be 1–64 characters")
            if "web_app" in button:
                webapp = button["web_app"]
                if not isinstance(webapp, dict) or set(webapp) != {"url"}:
                    raise ValidationError("Web App requires exactly one URL")
                safe_url(webapp["url"])
            else:
                safe_url(button["url"])
    if count > 32:
        raise ValidationError("Use at most 32 buttons")


@transaction.atomic
def activate_welcome(template: WelcomeTemplate, actor: Any, *, reason: str) -> None:
    if not actor.is_active or not actor.has_perm("notifications.activate_welcome"):
        raise PermissionDenied
    if not reason.strip():
        raise ValidationError("An activation or rollback reason is required")
    # Lock the immutable version as well as the singleton to serialize first activation.
    template = WelcomeTemplate.objects.select_for_update().get(pk=template.pk)
    template.full_clean()
    config, _ = WelcomeConfiguration.objects.get_or_create(pk=1)
    config = WelcomeConfiguration.objects.select_for_update().get(pk=config.pk)
    previous = config.active_template_id
    config.active_template = template
    config.save()
    OperatorAuditEvent.objects.create(
        actor=actor,
        action="WELCOME_ACTIVATE",
        target_type="WelcomeTemplate",
        target_id=str(template.pk),
        reason=reason,
        before_state={"template": previous},
        after_state={"template": template.pk, "version": template.version},
    )


@transaction.atomic
def test_send_welcome(
    template: WelcomeTemplate, actor: Any, *, reason: str, request_key: str
) -> None:
    if (
        not actor.is_active
        or not actor.has_perm("notifications.test_welcome")
        or not reason.strip()
    ):
        raise PermissionDenied
    operator = BotAdminOperator.objects.filter(user=actor, enabled=True).first()
    if not operator or operator.telegram_user_id not in settings.TELEGRAM_ADMIN_IDS:
        raise PermissionDenied
    player = Player.objects.filter(
        telegram_user_id=operator.telegram_user_id, first_private_contact_at__isnull=False
    ).first()
    if not player:
        raise ValidationError("Open the bot privately as the authorized operator first")
    template.full_clean()
    from apps.telegram.welcome import queue_private_message

    delivery = queue_private_message(
        player,
        key=f"welcome-test:{actor.pk}:{request_key}",
        text=template.body,
        options={
            "parse_mode": "HTML",
            "reply_markup": template.keyboard,
            "link_preview_options": {"is_disabled": True},
        },
    )
    OperatorAuditEvent.objects.create(
        actor=actor,
        action="WELCOME_TEST",
        target_type="TelegramDelivery",
        target_id=str(delivery.pk),
        reason=reason,
        after_state={"template": template.pk},
    )
