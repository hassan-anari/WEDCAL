"""Opt-in empty disposable rollback/forward smoke; never rewrites populated economics."""

import os

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.skipif(
    os.getenv("PHASE4_MIGRATION_CYCLE") != "1", reason="Isolated empty-schema migration cycle"
)
@pytest.mark.django_db(transaction=True)
def test_empty_disposable_backward_forward_cycle() -> None:
    assert connection.settings_dict["NAME"].startswith("test_phase4_migration")
    from apps.arena.models import EconomicEpoch

    assert not EconomicEpoch.objects.exists()
    executor = MigrationExecutor(connection)
    executor.migrate([("arena", "0019_phase3_deferred_evidence_fencing")])
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM arena_growthunlockevent")
        assert cursor.fetchone()[0] == 0
