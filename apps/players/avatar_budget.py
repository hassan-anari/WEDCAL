"""Bound avatar network work across processes; connection loss releases the PostgreSQL slot."""

from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from django.db import connection

AVATAR_REFRESH_LOCK_NAMESPACE = 15_042


@contextmanager
def avatar_refresh_slot() -> Iterator[bool]:
    slot = None
    for candidate in range(settings.AVATAR_CACHE_MAX_CONCURRENT_REFRESHES):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", [AVATAR_REFRESH_LOCK_NAMESPACE, candidate]
            )
            if cursor.fetchone()[0]:
                slot = candidate
                break
    try:
        yield slot is not None
    finally:
        if slot is not None:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", [AVATAR_REFRESH_LOCK_NAMESPACE, slot]
                )
