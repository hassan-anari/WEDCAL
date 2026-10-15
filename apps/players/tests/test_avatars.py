import io
import logging
import socket
import urllib.request
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from apps.players import avatars
from apps.players.models import PlayerAvatar


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, content: bytes) -> None:
        self.objects[key] = content

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture
def cache_world(settings: Any, monkeypatch: Any, player_factory: Any) -> Any:
    settings.AVATAR_CACHE_ENABLED = True
    settings.AVATAR_S3_BUCKET = "test-private"
    settings.AVATAR_PUBLIC_API_ORIGIN = "https://api.example.com"
    storage = MemoryStorage()
    monkeypatch.setattr(avatars, "get_storage", lambda: storage)
    monkeypatch.setattr(avatars, "reserve_bot_api_budget", lambda *args, **kwargs: None)
    player = player_factory()
    player.photo_url = "https://t.me/i/userpic/320/test.jpg"
    player.save()
    return player, storage


def photo() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(out, format="PNG")
    return out.getvalue()


@pytest.mark.django_db
def test_disabled_fallback_creates_no_metadata(player_factory: Any) -> None:
    player = player_factory()
    player.photo_url = "https://t.me/a.jpg"
    assert avatars.avatar_url(player) == player.photo_url
    assert not PlayerAvatar.objects.exists()


@pytest.mark.django_db
def test_verified_bot_refresh_switch_and_deletion(cache_world: Any, monkeypatch: Any) -> None:
    player, storage = cache_world
    calls: list[Any] = []

    def bot_call(method: str, params: Any) -> Any:
        calls.append((method, params))
        return (
            {"photos": [[{"file_id": "trusted", "file_size": 100}]]}
            if method == "getUserProfilePhotos"
            else {"file_path": "photos/valid.jpg"}
        )

    monkeypatch.setattr(avatars, "bot_call", bot_call)
    monkeypatch.setattr(avatars, "_telegram_request", lambda *args, **kwargs: photo())
    assert avatars.avatar_url(player) == player.photo_url
    avatars.refresh_player_avatar(player.pk)
    url = avatars.avatar_url(player)
    row = PlayerAvatar.objects.get(player=player)
    assert url.endswith(str(row.delivery_id)) and "t.me" not in url
    assert calls[0][1] == {"user_id": player.telegram_user_id, "limit": 1}
    response = avatars.deliver_avatar(row.delivery_id)
    assert response.status_code == 200 and response["Content-Type"] == "image/jpeg"
    old_id, old_key = row.delivery_id, row.object_key
    player.photo_url = "https://t.me/changed.jpg"
    player.save()
    assert avatars.deliver_avatar(old_id).status_code == 404
    assert avatars.avatar_url(player) == player.photo_url
    avatars.refresh_player_avatar(player.pk)
    row.refresh_from_db()
    assert row.delivery_id != old_id and old_key not in storage.objects
    assert avatars.deliver_avatar(uuid4()).status_code == 404
    assert avatars.purge_expired_avatars(at=timezone.now() + timedelta(days=8)) == 1
    assert not storage.objects


@pytest.mark.django_db
def test_cache_failure_privacy_and_photo_removal(
    cache_world: Any, monkeypatch: Any, caplog: Any
) -> None:
    player, storage = cache_world

    def fail(*args: Any) -> Any:
        raise ValueError("https://api.telegram.org/botSECRET/private-photo")

    monkeypatch.setattr(avatars, "retrieve_photo", fail)
    with caplog.at_level(logging.WARNING):
        avatars.refresh_player_avatar(player.pk)
    assert "SECRET" not in caplog.text
    assert avatars.avatar_url(player) == player.photo_url
    monkeypatch.setattr(avatars, "retrieve_photo", lambda *args: photo())
    PlayerAvatar.objects.filter(player=player).update(refresh_after=timezone.now())
    avatars.refresh_player_avatar(player.pk)
    assert storage.objects
    monkeypatch.setattr(avatars, "retrieve_photo", lambda *args: None)
    PlayerAvatar.objects.filter(player=player).update(refresh_after=timezone.now())
    avatars.refresh_player_avatar(player.pk)
    assert not storage.objects


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "https://127.0.0.1/a.jpg",
        "../secret.jpg",
        "photos/../../secret.jpg",
        "//localhost/a.jpg",
        "photos/evil.svg",
        "photos/a.jpg?secret=1",
        "http://169.254.169.254/metadata",
    ],
)
def test_no_arbitrary_sources_or_ssrf(cache_world: Any, monkeypatch: Any, path: str) -> None:
    player, _ = cache_world

    def call(method: str, params: Any) -> Any:
        return (
            {"photos": [[{"file_id": "one"}]]}
            if method == "getUserProfilePhotos"
            else {"file_path": path}
        )

    monkeypatch.setattr(avatars, "bot_call", call)
    with pytest.raises(ValueError, match="source_path"):
        avatars.retrieve_photo(player)


def test_image_bounds_mime_and_redirect_rejection(settings: Any) -> None:
    assert avatars.sanitize_image(photo()).startswith(b"\xff\xd8")
    with pytest.raises(UnidentifiedImageError):
        avatars.sanitize_image(b"<svg><script>bad</script></svg>")
    settings.AVATAR_CACHE_MAX_BYTES = 10
    with pytest.raises(ValueError, match="source_size"):
        avatars.sanitize_image(photo())
    handler: Any = avatars.NoRedirect()
    assert handler.redirect_request(None, None, 302, "", None, "http://localhost") is None


@pytest.mark.django_db
def test_delivery_storage_failure_is_safe(cache_world: Any, monkeypatch: Any) -> None:
    player, storage = cache_world
    monkeypatch.setattr(avatars, "retrieve_photo", lambda *args: photo())
    avatars.refresh_player_avatar(player.pk)
    row = PlayerAvatar.objects.get()
    storage.objects.clear()
    response = avatars.deliver_avatar(row.delivery_id)
    assert response.status_code == 503
    assert response["Cache-Control"] == "no-store" and response["Retry-After"] == "60"


@pytest.mark.django_db
def test_private_source_ip_and_response_mime_bounds(
    cache_world: Any, monkeypatch: Any, settings: Any
) -> None:
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
    )
    with pytest.raises(ValueError, match="source_address"):
        avatars._telegram_request("photos/a.jpg", image=True)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("149.154.167.220", 443))],
    )
    from email.message import Message

    class Response:
        status = 200
        headers = Message()

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    Response.headers["Content-Type"] = "text/html"
    with pytest.raises(ValueError, match="source_mime"):
        avatars._telegram_request("photos/a.jpg", image=True)
    Response.headers.replace_header("Content-Type", "image/jpeg")
    Response.headers["Content-Length"] = str(settings.AVATAR_CACHE_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="source_size"):
        avatars._telegram_request("photos/a.jpg", image=True)


def test_avatar_celery_tasks_are_discoverable() -> None:
    from dexo_land.celery import app

    app.autodiscover_tasks(force=True)
    assert "apps.players.tasks.refresh_due_avatars" in app.tasks
    assert "apps.players.tasks.purge_avatars" in app.tasks


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("mime", "image_format", "body", "expected_error"),
    [
        ("application/octet-stream", "JPEG", None, None),
        ("image/jpeg", "JPEG", None, None),
        ("image/png", "PNG", None, None),
        ("image/webp", "WEBP", None, None),
        ("application/octet-stream", "JPEG", b"invalid image bytes", UnidentifiedImageError),
        (
            "application/octet-stream",
            "JPEG",
            b"<html><script>bad</script></html>",
            UnidentifiedImageError,
        ),
        ("text/html", "JPEG", b"<html>not a Telegram photo</html>", ValueError),
    ],
)
def test_telegram_octet_stream_still_requires_sanitized_real_image(
    cache_world: Any,
    monkeypatch: Any,
    settings: Any,
    mime: str,
    image_format: str,
    body: bytes | None,
    expected_error: Any,
) -> None:
    from email.message import Message

    player, _ = cache_world
    settings.TELEGRAM_BOT_TOKEN = "123456:test-token"
    if body is None:
        original = Image.new("RGB", (600, 600), "blue")
        exif = original.getexif()
        exif[270] = "synthetic metadata must be stripped"
        encoded = io.BytesIO()
        original.save(encoded, format=image_format, exif=exif)
        body = encoded.getvalue()
    assert body is not None
    stream = io.BytesIO(body)
    headers = Message()
    headers["Content-Type"] = mime
    headers["Content-Length"] = str(len(body))

    class Response:
        status = 200

        def __init__(self) -> None:
            self.headers = headers

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def read(self, limit: int) -> bytes:
            return stream.read(limit)

    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            return Response()

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("149.154.167.220", 443))]
    )
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(
        avatars,
        "bot_call",
        lambda method, params: (
            {"photos": [[{"file_id": "trusted"}]]}
            if method == "getUserProfilePhotos"
            else {"file_path": "photos/valid.jpg"}
        ),
    )
    if expected_error:
        with pytest.raises(expected_error):
            avatars.retrieve_photo(player)
    else:
        sanitized = avatars.retrieve_photo(player)
        assert sanitized is not None
        with Image.open(io.BytesIO(sanitized)) as output:
            assert output.format == "JPEG"
            assert max(output.size) <= 512
            assert not output.getexif()
