import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Report PostgreSQL relation/table/index sizes and row estimates without mutation"

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        if connection.vendor != "postgresql":
            raise CommandError("production_data_report requires PostgreSQL")
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT relname, n_live_tup, pg_total_relation_size(relid),
                          pg_indexes_size(relid)
                   FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC"""
            )
            rows = [
                {
                    "table": name,
                    "estimated_rows": count,
                    "total_bytes": total,
                    "index_bytes": indexes,
                }
                for name, count, total, indexes in cursor.fetchall()
            ]
        self.stdout.write(json.dumps(rows, indent=2))
