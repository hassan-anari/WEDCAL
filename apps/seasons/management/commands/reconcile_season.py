import json

from django.core.management.base import BaseCommand

from apps.seasons.close import reconcile_season_dlx, reconcile_season_points
from apps.seasons.models import Season


class Command(BaseCommand):
    help = "Dry-run canonical XP/DLX reconciliation; optional accepted XP projection repair"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("season")
        parser.add_argument("--repair-xp", action="store_true")

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        season = Season.objects.get(code=options["season"])
        result = {
            "xp": reconcile_season_points(season, repair=options["repair_xp"]),
            "dlx": reconcile_season_dlx(season),
        }
        self.stdout.write(json.dumps(result, indent=2, default=str))
