# mypy: disable-error-code="no-untyped-def,type-arg"
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError

from apps.conftest import BASE_CONFIG
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import preview_safe_cutover, publish_game_config

pytestmark = pytest.mark.django_db(transaction=True)


def draft(version: str, effective_at: datetime, payload: dict) -> GameConfigVersion:
    return GameConfigVersion.objects.create(
        version=version,
        schema_version=payload["meta"]["schema_version"],
        effective_at=effective_at,
        payload=payload,
    )


def test_base_tap_mid_day_cutover_rejected_and_boundary_accepted():
    base_at = datetime(2026, 1, 1, tzinfo=UTC)
    base = draft("cutover-base", base_at, deepcopy(BASE_CONFIG))
    publish_game_config(base, published_at=base_at - timedelta(seconds=1))
    changed = deepcopy(BASE_CONFIG)
    changed["base_tap"]["rewarded_taps_per_utc_day"] += 1
    unsafe = draft("cutover-unsafe", base_at + timedelta(hours=1), changed)
    assert preview_safe_cutover(unsafe)
    with pytest.raises(ValidationError):
        publish_game_config(unsafe, published_at=base_at + timedelta(minutes=30))
    unsafe.delete()
    safe = draft("cutover-safe", base_at + timedelta(days=1), changed)
    assert preview_safe_cutover(safe) == ()
    publish_game_config(safe, published_at=base_at + timedelta(hours=23))


def test_database_guard_rejects_model_queryset_and_sql_publication():
    from django.db import DatabaseError, connection, transaction

    candidate = draft("guard-candidate", datetime(2026, 2, 1, tzinfo=UTC), deepcopy(BASE_CONFIG))
    candidate.status = GameConfigVersion.Status.PUBLISHED
    with pytest.raises(DatabaseError), transaction.atomic():
        candidate.save(update_fields=("status",))
    with pytest.raises(DatabaseError), transaction.atomic():
        GameConfigVersion.objects.filter(pk=candidate.pk).update(status="PUBLISHED")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE game_config_gameconfigversion SET status='PUBLISHED' WHERE id=%s",
            [candidate.pk],
        )
    candidate.refresh_from_db()
    assert candidate.status == GameConfigVersion.Status.DRAFT
