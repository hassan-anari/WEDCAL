"""Canonical Season finalization synchronization boundary.

Lock order is advisory Season barrier first, then ordinary model row locks. Writers take the shared
transaction lock; close takes the exclusive transaction lock and rechecks durable providers before
persisting the barrier marker. PostgreSQL releases either lock at transaction end.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from django.core.exceptions import ValidationError
from django.db import connection

from apps.seasons.models import SeasonCloseRun

LOCK_NAMESPACE = 11_042


class SeasonFinalizationEstablished(ValidationError):
    pass


def acquire_economic_write_barrier(season_id: int) -> None:
    """Join the shared barrier; caller must already be inside its atomic write transaction."""
    if not connection.in_atomic_block:
        raise RuntimeError("Economic barrier must be acquired inside transaction.atomic()")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", [LOCK_NAMESPACE, season_id])
    if SeasonCloseRun.objects.filter(
        season_id=season_id, barrier_established_at__isnull=False
    ).exists():
        raise SeasonFinalizationEstablished(
            "Season finalization barrier is established; economic mutation rejected"
        )


def acquire_close_barrier(season_id: int) -> None:
    if not connection.in_atomic_block:
        raise RuntimeError("Close barrier must be acquired inside transaction.atomic()")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [LOCK_NAMESPACE, season_id])


@contextmanager
def economic_write_session_barrier(season_id: int) -> Iterator[None]:
    """Hold the shared barrier across workflows that intentionally use multiple transactions."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock_shared(%s, %s)", [LOCK_NAMESPACE, season_id])
    try:
        if SeasonCloseRun.objects.filter(
            season_id=season_id, barrier_established_at__isnull=False
        ).exists():
            raise SeasonFinalizationEstablished(
                "Season finalization barrier is established; economic workflow rejected"
            )
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock_shared(%s, %s)", [LOCK_NAMESPACE, season_id])
