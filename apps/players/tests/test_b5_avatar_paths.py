# ruff: noqa: F811
# mypy: disable-error-code="no-untyped-def"
import io

import pytest
from PIL import Image

from apps.players import avatars, group_avatars
from apps.players.models import GroupAvatar
from apps.players.tests.test_b4_group_images import image_world  # noqa: F401


@pytest.mark.parametrize(
    "path",
    [
        "photos/valid.jpg",
        "profile_photos/file_123.jpg",
        "profile_photos/a-b_C.jpeg",
        "photos/sub/valid.webp",
    ],
)
def test_verified_telegram_paths(path, monkeypatch):
    raw = io.BytesIO()
    Image.new("RGB", (10, 10)).save(raw, format="PNG")
    calls = []
    monkeypatch.setattr(avatars, "bot_call", lambda *_: {"file_path": path})

    def download(value, **kwargs):
        calls.append((value, kwargs))
        return raw.getvalue()

    monkeypatch.setattr(avatars, "_telegram_request", download)
    assert avatars.retrieve_file("synthetic-private-id").startswith(b"\xff\xd8")
    assert calls == [(path, {"image": True})]


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/a.jpg",
        "/profile_photos/a.jpg",
        "profile_photos/../a.jpg",
        "profile_photos/a/../../b.jpg",
        "profile_photos//a.jpg",
        "profile_photos/%2e%2e/a.jpg",
        "profile_photos/a.jpg?x=1",
        "profile_photos/a.jpg#x",
        "profile_photos/a.jpg\n",
        "profile_photos/\\a.jpg",
        "profile_photos/a.svg",
        "documents/a.jpg",
        "profile_photos/.jpg",
        None,
    ],
)
def test_invalid_telegram_paths_never_download(path, monkeypatch):
    monkeypatch.setattr(avatars, "bot_call", lambda *_: {"file_path": path})
    monkeypatch.setattr(
        avatars, "_telegram_request", lambda *_args, **_kw: pytest.fail("unsafe download")
    )
    with pytest.raises(avatars.AvatarSourceError, match="source_path"):
        avatars.retrieve_file("synthetic-private-id")


@pytest.mark.django_db(transaction=True)
def test_group_profile_photos_cache_and_safe_error_categories(image_world, monkeypatch, caplog):
    group, storage = image_world
    monkeypatch.setattr(avatars, "bot_call", lambda *_: {"file_path": "profile_photos/group.jpg"})
    group_avatars.group_avatar_url(group)
    group_avatars.refresh_group_avatar(group.pk)
    assert GroupAvatar.objects.get().object_key in storage.objects
    from django.utils import timezone

    for category in ["source_path", "source_mime", "source_size"]:
        GroupAvatar.objects.update(refresh_after=timezone.now())

        def fail(*_, error=category):
            raise avatars.AvatarSourceError(error)

        monkeypatch.setattr(group_avatars, "retrieve_file", fail)
        group_avatars.refresh_group_avatar(group.pk)
        assert GroupAvatar.objects.get().last_error_category == category
        assert category in caplog.text
    assert "synthetic-private-id" not in caplog.text
    assert avatars.error_category(ValueError("botSECRET/private-id")) == "ValueError"
