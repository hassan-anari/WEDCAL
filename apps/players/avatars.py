import hashlib
import io
import ipaddress
import json
import random
import re
import socket
import urllib.error
import urllib.request
from contextlib import suppress
from datetime import datetime, timedelta
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import structlog
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from PIL import Image

from apps.players.avatar_budget import avatar_refresh_slot
from apps.players.avatar_storage import get_storage
from apps.players.models import Player, PlayerAvatar
from apps.telegram.budget import defer_bot_api, reserve_bot_api_budget, retry_after_from_payload

logger = structlog.get_logger(__name__)
FILE_PATH = re.compile(r"^photos/[A-Za-z0-9_/-]+\.(?:jpg|jpeg|png|webp)$")


def configured() -> bool:
    origin = urlsplit(settings.AVATAR_PUBLIC_API_ORIGIN)
    endpoint = urlsplit(settings.AVATAR_S3_ENDPOINT_URL)
    return bool(
        settings.AVATAR_CACHE_ENABLED
        and 1 <= settings.AVATAR_CACHE_MAX_CONCURRENT_REFRESHES <= 16
        and 1 <= settings.AVATAR_CACHE_TTL_SECONDS <= 2592000
        and 1 <= settings.AVATAR_CACHE_MAX_BYTES <= 5242880
        and 1 <= settings.AVATAR_CACHE_MAX_PIXELS <= 16000000
        and settings.AVATAR_S3_BUCKET
        and origin.scheme == "https"
        and origin.hostname
        and not origin.username
        and not origin.query
        and not origin.fragment
        and origin.path in ("", "/")
        and (not settings.AVATAR_S3_ENDPOINT_URL or endpoint.scheme == "https")
    )


def fingerprint(player: Player) -> str:
    # Signed Telegram metadata triggers refresh; it is never used as a fetch URL.
    return hashlib.sha256(player.photo_url.encode()).hexdigest()


def avatar_url(player: Player) -> str:
    if not configured():
        return player.photo_url
    row = PlayerAvatar.objects.filter(player=player).first()
    now = timezone.now()
    if row is None:
        row, _ = PlayerAvatar.objects.get_or_create(
            player=player,
            defaults={"source_fingerprint": fingerprint(player), "refresh_after": now},
        )
    elif (
        row.source_fingerprint != fingerprint(player)
        and row.refresh_token is None
        and not (row.last_error_category and row.refresh_after > now)
    ):
        PlayerAvatar.objects.filter(pk=row.pk, refresh_token__isnull=True).update(refresh_after=now)
    # Beat discovers due metadata; no broker or network call can delay Bootstrap.
    if (
        row
        and row.source_fingerprint == fingerprint(player)
        and row.object_key
        and row.expires_at
        and row.expires_at > now
    ):
        origin = settings.AVATAR_PUBLIC_API_ORIGIN.rstrip("/")
        return f"{origin}/api/v1/profile/avatar/{row.delivery_id}"
    return player.photo_url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class AvatarRetry(Exception):
    def __init__(self, category: str, retry_after: int) -> None:
        self.category = category
        self.retry_after = retry_after
        super().__init__(category)


def _rate_limit(retry_after: int | None) -> AvatarRetry:
    delay = retry_after or settings.TELEGRAM_API_DEFAULT_RETRY_SECONDS
    defer_bot_api(delay)
    return AvatarRetry("rate_limited", delay)


def _telegram_request(
    path: str, *, parameters: dict[str, Any] | None = None, image: bool = False
) -> bytes:
    # Only server-constructed api.telegram.org Bot API/file URLs; no photo_url ingestion.
    if not settings.TELEGRAM_BOT_TOKEN or not re.fullmatch(
        r"[0-9]+:[A-Za-z0-9_-]+", settings.TELEGRAM_BOT_TOKEN
    ):
        raise ValueError("bot_configuration")
    denial = reserve_bot_api_budget("avatarFileDownload" if image else path, fail_closed=True)
    if denial is not None:
        raise AvatarRetry(denial.category, denial.retry_after)
    for addr in socket.getaddrinfo("api.telegram.org", 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(addr[4][0]).is_global:
            raise ValueError("source_address")
    prefix = "file/bot" if image else "bot"
    url = f"https://api.telegram.org/{prefix}{settings.TELEGRAM_BOT_TOKEN}/{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(parameters).encode() if parameters is not None else None,
        headers={"Content-Type": "application/json"} if parameters is not None else {},
    )
    opener = urllib.request.build_opener(NoRedirect())
    limit = settings.AVATAR_CACHE_MAX_BYTES if image else 65536
    started = monotonic()
    try:
        with opener.open(req, timeout=5) as response:
            if response.status != 200:
                raise ValueError("source_status")
            mime = response.headers.get_content_type()
            if image and mime not in {
                "image/jpeg",
                "image/png",
                "image/webp",
                "application/octet-stream",
            }:
                raise ValueError("source_mime")
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise ValueError("source_size")
            result = bytearray()
            while len(result) <= limit:
                chunk = response.read(min(65536, limit + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
                if monotonic() - started > 10:
                    raise ValueError("source_timeout")
            if len(result) > limit:
                raise ValueError("source_size")
            return bytes(result)
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            raise
        delay = None
        with suppress(ValueError, TypeError):
            delay = retry_after_from_payload(json.loads(exc.read(65536)))
        if delay is None:
            value = exc.headers.get("Retry-After", "")
            delay = int(value) if value.isdigit() and int(value) > 0 else None
        raise _rate_limit(delay) from None


def bot_call(method: str, parameters: dict[str, Any]) -> dict[str, Any]:
    if method not in {"getUserProfilePhotos", "getFile", "getChat"}:
        raise ValueError("source_method")
    payload = json.loads(_telegram_request(method, parameters=parameters))
    if isinstance(payload, dict) and payload.get("error_code") == 429:
        raise _rate_limit(retry_after_from_payload(payload))
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or not isinstance(payload.get("result"), dict)
    ):
        raise ValueError("bot_response")
    return payload["result"]  # type: ignore[no-any-return]


def retrieve_photo(player: Player) -> bytes | None:
    photos = bot_call("getUserProfilePhotos", {"user_id": player.telegram_user_id, "limit": 1}).get(
        "photos", []
    )
    if not photos:
        return None
    photo = photos[0][-1]
    if photo.get("file_size", 0) > settings.AVATAR_CACHE_MAX_BYTES:
        raise ValueError("source_size")
    return retrieve_file(photo["file_id"])


def retrieve_file(file_id: str) -> bytes:
    remote = bot_call("getFile", {"file_id": file_id})
    path = remote.get("file_path", "")
    if not isinstance(path, str) or not FILE_PATH.fullmatch(path) or ".." in path or "//" in path:
        raise ValueError("source_path")
    content = _telegram_request(path, image=True)
    return sanitize_image(content)


def sanitize_image(content: bytes) -> bytes:
    if len(content) > settings.AVATAR_CACHE_MAX_BYTES:
        raise ValueError("source_size")
    with Image.open(io.BytesIO(content)) as image:
        if (
            image.format not in {"JPEG", "PNG", "WEBP"}
            or image.width * image.height > settings.AVATAR_CACHE_MAX_PIXELS
        ):
            raise ValueError("image_format_or_pixels")
        image.load()
        normalized = image.convert("RGB")
        normalized.thumbnail((512, 512))
        output = io.BytesIO()
        normalized.save(output, format="JPEG", quality=85)
        result = output.getvalue()
        if len(result) > settings.AVATAR_CACHE_MAX_BYTES:
            raise ValueError("image_size")
        return result


def refresh_player_avatar(player_id: int, *, at: datetime | None = None) -> None:
    if not configured():
        return
    now = at or timezone.now()
    with avatar_refresh_slot() as available:
        if not available:
            # Never hold a worker sleeping or reset another worker's in-progress token.
            PlayerAvatar.objects.filter(
                player_id=player_id, refresh_token__isnull=True, refresh_after__lte=now
            ).update(
                refresh_after=now + timedelta(seconds=settings.AVATAR_CACHE_RETRY_SECONDS),
                last_error_category="concurrency_limited",
            )
            return
        _refresh_player_avatar(player_id, at=now)


def _refresh_player_avatar(player_id: int, *, at: datetime) -> None:
    if not configured():
        return
    now = at
    token = uuid4()
    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player_id)
        row, _ = PlayerAvatar.objects.get_or_create(
            player=player,
            defaults={"source_fingerprint": fingerprint(player), "refresh_after": now},
        )
        row = PlayerAvatar.objects.select_for_update().get(pk=row.pk)
        if row.refresh_after > now:
            return
        row.refresh_token = token
        row.refresh_after = now + timedelta(seconds=settings.AVATAR_CACHE_RETRY_SECONDS)
        row.save()
        source = fingerprint(player)
    key = f"avatars/{uuid4()}.jpg"
    try:
        storage = get_storage()
        content = retrieve_photo(player)
        if content is not None:
            storage.put(key, content)
        else:
            key = ""
        with transaction.atomic():
            current_player = Player.objects.select_for_update().get(pk=player_id)
            row = PlayerAvatar.objects.select_for_update().get(player_id=player_id)
            if row.refresh_token != token or fingerprint(current_player) != source:
                if key:
                    storage.delete(key)
                return
            old_key = row.object_key
            row.delivery_id = uuid4()
            row.object_key = key
            row.source_fingerprint = source
            row.expires_at = now + timedelta(seconds=settings.AVATAR_CACHE_TTL_SECONDS)
            row.refresh_after = row.expires_at
            row.refresh_token = None
            row.last_error_category = ""
            row.save()
        storage.delete(old_key)
    except Exception as exc:
        # Category only: urllib/boto exceptions can contain bot tokens/private paths.
        delay = max(
            settings.AVATAR_CACHE_RETRY_SECONDS,
            exc.retry_after if isinstance(exc, AvatarRetry) else 0,
        )
        delay += random.randint(0, max(0, settings.TELEGRAM_API_RETRY_JITTER_SECONDS))
        category = exc.category if isinstance(exc, AvatarRetry) else type(exc).__name__[:40]
        PlayerAvatar.objects.filter(player_id=player_id, refresh_token=token).update(
            refresh_token=None,
            last_error_category=category,
            refresh_after=now + timedelta(seconds=delay),
        )
        logger.warning("avatar_refresh_failed", player_id=player_id, category=category)


def deliver_avatar(delivery_id: UUID) -> HttpResponse:
    if not configured():
        return HttpResponse(status=404)
    row = (
        PlayerAvatar.objects.select_related("player")
        .filter(delivery_id=delivery_id, expires_at__gt=timezone.now())
        .first()
    )
    if row is None or not row.object_key or row.source_fingerprint != fingerprint(row.player):
        return HttpResponse(status=404)
    try:
        data = get_storage().get(row.object_key)
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


def purge_expired_avatars(*, at: datetime, limit: int = 100) -> int:
    if not configured():
        return 0
    storage = get_storage()
    ids = list(
        PlayerAvatar.objects.filter(expires_at__lte=at)
        .order_by("expires_at")
        .values_list("pk", flat=True)[:limit]
    )
    count = 0
    for pk in ids:
        with transaction.atomic():
            row = PlayerAvatar.objects.select_for_update().filter(pk=pk, expires_at__lte=at).first()
            if row is None:
                continue
            try:
                storage.delete(row.object_key)
            except Exception:
                continue
            row.object_key = ""
            row.expires_at = None
            row.delivery_id = uuid4()
            row.save()
            count += 1
    return count
