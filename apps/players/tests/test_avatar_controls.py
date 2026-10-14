from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import Any

import pytest
from django.db import connections
from django.utils import timezone

from apps.players import avatars
from apps.players.models import PlayerAvatar
from apps.players.tests.test_avatars import cache_world  # noqa: F401


@pytest.mark.django_db
def test_rate_limit_retry_is_persisted_and_cannot_be_bypassed_by_bootstrap(
    cache_world: Any,  # noqa: F811
    settings: Any,
    monkeypatch: Any,
) -> None:
    player, _ = cache_world
    settings.AVATAR_CACHE_RETRY_SECONDS = 60
    settings.TELEGRAM_API_RETRY_JITTER_SECONDS = 0
    now = timezone.now()
    calls: list[int] = []

    def limited(player: Any) -> Any:
        calls.append(player.pk)
        raise avatars.AvatarRetry("rate_limited", 1234)

    monkeypatch.setattr(avatars, "retrieve_photo", limited)
    avatars.refresh_player_avatar(player.pk, at=now)
    row = PlayerAvatar.objects.get(player=player)
    assert row.refresh_token is None and row.last_error_category == "rate_limited"
    assert row.refresh_after == now + timedelta(seconds=1234)
    player.photo_url = "https://t.me/new-photo.jpg"
    player.save()
    assert avatars.avatar_url(player) == player.photo_url
    row.refresh_from_db()
    assert row.refresh_after == now + timedelta(seconds=1234)
    avatars.refresh_player_avatar(player.pk, at=now + timedelta(seconds=1233))
    assert len(calls) == 1
    avatars.refresh_player_avatar(player.pk, at=row.refresh_after)
    assert len(calls) == 2


@pytest.mark.django_db(transaction=True)
def test_postgres_slot_bounds_avatar_workers_and_releases_after_success(
    cache_world: Any,  # noqa: F811
    player_factory: Any,
    settings: Any,
    monkeypatch: Any,
) -> None:
    settings.AVATAR_CACHE_MAX_CONCURRENT_REFRESHES = 1
    first, _ = cache_world
    second = player_factory(1984)
    now = timezone.now()
    PlayerAvatar.objects.create(
        player=second, source_fingerprint=avatars.fingerprint(second), refresh_after=now
    )
    entered, release = Event(), Event()
    seen: list[int] = []

    def retrieve(player: Any) -> None:
        seen.append(player.pk)
        if player.pk == first.pk:
            entered.set()
            assert release.wait(10)
        return None

    monkeypatch.setattr(avatars, "retrieve_photo", retrieve)

    def worker() -> None:
        connections.close_all()
        try:
            avatars.refresh_player_avatar(first.pk, at=now)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(worker)
        assert entered.wait(5)
        try:
            avatars.refresh_player_avatar(second.pk, at=now)
            assert seen == [first.pk]
            row = PlayerAvatar.objects.get(player=second)
            assert row.last_error_category == "concurrency_limited"
            assert row.refresh_after > now
        finally:
            release.set()
        task.result(timeout=10)
    # The completed worker's session lock is released. The deferred worker can run at its due time.
    avatars.refresh_player_avatar(second.pk, at=row.refresh_after)
    assert seen == [first.pk, second.pk]
