from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.seasons.barrier import (
    SeasonFinalizationEstablished,
    acquire_close_barrier,
    acquire_economic_write_barrier,
)
from apps.seasons.models import SeasonCloseRun


@pytest.mark.django_db(transaction=True)
def test_exclusive_barrier_waits_for_existing_writer_then_rejects_new_writer(
    phase4_config_factory: Any, season_factory: Any
) -> None:
    season = season_factory(phase4_config_factory())
    run = SeasonCloseRun.objects.create(
        season=season,
        idempotency_key=f"stage2-barrier:{season.pk}",
        cutoff_at=timezone.now(),
        service_actor="stage2-test",
        reason="concurrency verification",
        correlation_id=uuid4(),
    )
    writer_locked = Event()
    release_writer = Event()
    close_locked = Event()

    def writer() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                acquire_economic_write_barrier(season.pk)
                writer_locked.set()
                assert release_writer.wait(timeout=5)
        finally:
            close_old_connections()

    def closer() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                acquire_close_barrier(season.pk)
                close_locked.set()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('dexo.season_close_barrier', 'on', true)")
                locked = SeasonCloseRun.objects.select_for_update().get(pk=run.pk)
                locked.barrier_established_at = timezone.now()
                locked.save(update_fields=("barrier_established_at", "updated_at"))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer_future = pool.submit(writer)
        assert writer_locked.wait(timeout=5)
        close_future = pool.submit(closer)
        assert not close_locked.wait(timeout=0.25)
        release_writer.set()
        writer_future.result(timeout=5)
        close_future.result(timeout=5)

    run.refresh_from_db()
    assert run.barrier_established_at is not None
    with pytest.raises(SeasonFinalizationEstablished), transaction.atomic():
        acquire_economic_write_barrier(season.pk)


@pytest.mark.django_db(transaction=True)
def test_season_barriers_are_scoped_and_do_not_cross_block(
    phase4_config_factory: Any, season_factory: Any, close_season_for_test: Any
) -> None:
    config = phase4_config_factory()
    first = season_factory(config)
    close_season_for_test(first)
    second_start = first.ends_at + timedelta(seconds=1)
    second = season_factory(
        config, starts_at=second_start, ends_at=second_start + timedelta(days=45)
    )
    with transaction.atomic():
        acquire_economic_write_barrier(first.pk)
        acquire_close_barrier(second.pk)
