# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import io
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone
from PIL import Image

from apps.network.models import CreatorGroupRegistration
from apps.players import avatars, group_avatars
from apps.players.avatar_storage import S3GroupAvatarStorage
from apps.players.models import GroupAvatar, PlayerAvatar
from apps.telegram.models import TelegramGroup

pytestmark = pytest.mark.django_db(transaction=True)


class Storage:
    def __init__(self):
        self.objects = {}

    def put(self, key, value):
        assert key.startswith("group-images/")
        self.objects[key] = value

    def get(self, key):
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)


@pytest.fixture
def image_world(settings, monkeypatch, player_factory):
    settings.GROUP_IMAGE_CACHE_ENABLED = True
    settings.GROUP_IMAGE_S3_BUCKET = "isolated-private-bucket"
    settings.GROUP_IMAGE_S3_ENDPOINT_URL = "https://s3.example.com"
    settings.GROUP_IMAGE_S3_ACCESS_KEY_ID = "synthetic-group-key"
    settings.GROUP_IMAGE_S3_SECRET_ACCESS_KEY = "synthetic-group-secret"
    settings.AVATAR_PUBLIC_API_ORIGIN = "https://api.example.com"
    now = timezone.now()
    group = TelegramGroup.objects.create(
        telegram_chat_id=-10001,
        chat_type="supergroup",
        title="Real registered group",
        first_seen_at=now,
        last_seen_at=now,
    )
    CreatorGroupRegistration.objects.create(group=group, creator=player_factory())
    storage = Storage()
    monkeypatch.setattr(group_avatars, "S3GroupAvatarStorage", lambda: storage)
    image = io.BytesIO()
    Image.new("RGB", (40, 40), "red").save(image, format="PNG")
    monkeypatch.setattr(
        group_avatars,
        "bot_call",
        lambda method, params: {
            "id": group.telegram_chat_id,
            "photo": {"big_file_id": "real-telegram-file"},
        },
    )
    monkeypatch.setattr(
        avatars, "bot_call", lambda method, params: {"file_path": "photos/group.jpg"}
    )
    monkeypatch.setattr(avatars, "_telegram_request", lambda *args, **kwargs: image.getvalue())
    return group, storage


def test_group_image_uses_shared_sanitization_private_delivery_expiry_and_fallback(image_world):
    group, storage = image_world
    assert group_avatars.group_avatar_url(group) == ""
    group_avatars.refresh_group_avatar(group.pk)
    row = GroupAvatar.objects.get()
    assert row.object_key.startswith("group-images/")
    assert storage.objects[row.object_key].startswith(b"\xff\xd8")
    url = group_avatars.group_avatar_url(group)
    assert url.startswith("https://api.example.com/api/v1/profile/group-avatar/")
    assert "telegram" not in url and "synthetic" not in url
    response = group_avatars.deliver_group_avatar(row.delivery_id)
    assert response.status_code == 200 and response["Cache-Control"].startswith("private")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert group_avatars.deliver_group_avatar(uuid4()).status_code == 404
    old_delivery = row.delivery_id
    GroupAvatar.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert group_avatars.group_avatar_url(group) == ""
    assert group_avatars.deliver_group_avatar(old_delivery).status_code == 404
    assert group_avatars.purge_group_avatars() == 1 and not storage.objects


def test_group_image_failure_is_private_and_removal_invalidates_old_capability(
    image_world, monkeypatch, caplog
):
    group, storage = image_world
    group_avatars.group_avatar_url(group)
    group_avatars.refresh_group_avatar(group.pk)
    row = GroupAvatar.objects.get()
    old = row.delivery_id
    GroupAvatar.objects.update(refresh_after=timezone.now())
    monkeypatch.setattr(group_avatars, "bot_call", lambda *args: {"id": group.telegram_chat_id})
    group_avatars.refresh_group_avatar(group.pk)
    assert not storage.objects and group_avatars.deliver_group_avatar(old).status_code == 404

    def fail(*args):
        raise ValueError("https://api.telegram.org/botSECRET/path")

    monkeypatch.setattr(group_avatars, "bot_call", fail)
    GroupAvatar.objects.update(refresh_after=timezone.now())
    group_avatars.refresh_group_avatar(group.pk)
    assert "SECRET" not in caplog.text
    assert GroupAvatar.objects.get().last_error_category == "ValueError"
    assert GroupAvatar.objects.get().refresh_after > timezone.now()


def test_group_credentials_never_inherit_avatar_credentials(image_world, settings, monkeypatch):
    settings.GROUP_IMAGE_S3_ACCESS_KEY_ID = ""
    assert not group_avatars.configured()
    with pytest.raises(ValueError, match="credentials_required"):
        S3GroupAvatarStorage()
    settings.GROUP_IMAGE_S3_ACCESS_KEY_ID = "synthetic-group-key"
    calls = []
    monkeypatch.setattr(
        "apps.players.avatar_storage.boto3.client", lambda *a, **kw: calls.append(kw)
    )
    storage = S3GroupAvatarStorage()
    assert calls[0]["aws_access_key_id"] == "synthetic-group-key"
    assert calls[0]["aws_secret_access_key"] == "synthetic-group-secret"
    assert "aws_session_token" not in calls[0]
    with pytest.raises(ValueError, match="key_scope"):
        storage.get("avatars/forbidden.jpg")


def test_start_only_player_can_use_cached_telegram_avatar_without_miniapp_photo_metadata(
    settings, monkeypatch, player_factory
):
    settings.AVATAR_CACHE_ENABLED = True
    settings.AVATAR_PUBLIC_API_ORIGIN = "https://api.example.com"
    settings.AVATAR_S3_BUCKET = "private"
    player = player_factory()
    assert not player.photo_url and player.first_mini_app_at is None
    assert avatars.avatar_url(player) == ""

    class PlayerStorage(Storage):
        def put(self, key, value):
            assert key.startswith("avatars/")
            self.objects[key] = value

    storage = PlayerStorage()
    monkeypatch.setattr(avatars, "get_storage", lambda: storage)
    monkeypatch.setattr(
        avatars, "retrieve_photo", lambda *args: b"\xff\xd8synthetic-normalized-image"
    )
    avatars.refresh_player_avatar(player.pk)
    row = PlayerAvatar.objects.get()
    assert row.source_fingerprint == avatars.fingerprint(player)
    assert avatars.avatar_url(player).startswith("https://api.example.com/api/v1/profile/avatar/")
