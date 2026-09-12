import pytest
from django.conf import settings

from apps.clans.api import clan_out
from dexo_land.api import api


def test_clan_window_worker_is_scheduled() -> None:
    assert (
        settings.CELERY_BEAT_SCHEDULE["finalize-clan-activity-windows"]["task"]
        == "apps.clans.tasks.finalize_clan_activity_windows"
    )


def test_phase8_openapi_surface_is_plural_and_typed() -> None:
    paths = api.get_openapi_schema()["paths"]
    for path in [
        "/api/v1/clans/home",
        "/api/v1/clans/contributions",
        "/api/v1/clans/treasury",
        "/api/v1/clans/shop",
        "/api/v1/clans/inventory",
        "/api/v1/clans/missions",
    ]:
        assert path in paths
    assert not any(path.startswith("/api/v1/clan/") for path in paths)


@pytest.mark.django_db
def test_clan_serializer_uses_canonical_effective_capacity(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, _, clan = clan_world
    cs = clan()
    cs.capacity_bonus = 100
    cs.save()
    assert resolved.clan is not None
    assert clan_out(cs).capacity == resolved.clan.max_capacity_including_items


@pytest.mark.django_db
def test_shop_api_catalog_excludes_future_items(clan_world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, resolved, _, _ = clan_world
    assert resolved.clan is not None
    visible = {item.item_id for item in resolved.clan.inventory_items if item.available_s1}
    assert "alliance-permit" not in visible and "event-entry-pass" not in visible


@pytest.mark.django_db
def test_window_worker_isolates_poison_and_progresses(clan_world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from django.utils import timezone

    import apps.clans.windowing as windowing
    from apps.clans.models import ClanActivity, ClanMembershipEvent
    from apps.clans.windowing import finalize_due_windows

    now, _, _, _, _, clan = clan_world
    cs = clan()
    type(cs).objects.filter(pk=cs.pk).update(created_at=now - timedelta(hours=3))
    cs.refresh_from_db()
    member = cs.memberships.get()
    member.started_at = now - timedelta(hours=3)
    member.save()
    ClanMembershipEvent.objects.create(
        membership=member, kind="ACTIVATED", occurred_at=member.started_at
    )
    for message_id, offset in [(801, 120), (802, 90)]:
        ClanActivity.objects.create(
            clan_season=cs,
            membership=member,
            sender=member.player,
            group=cs.clan.telegram_group,
            season=cs.season,
            config_version=cs.config_version,
            telegram_message_id=message_id,
            telegram_update_id=message_id,
            observed_at=now - timedelta(minutes=offset),
            character_count=150,
            rewardable_chars=150,
            classification="VALID",
        )
    original = windowing._finalize
    calls = 0

    def sometimes_poison(activity, at):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("poison")
        return original(activity, at)

    monkeypatch.setattr(windowing, "_finalize", sometimes_poison)
    assert finalize_due_windows(limit=2, now=timezone.now()) == 1
