from django.core.management.base import BaseCommand, CommandError

from apps.arena.models import RankingProjectionEvent
from apps.network.models import NetworkActivity
from apps.rewards.models import RewardLedger
from apps.seasons.models import PlayerSeasonFinalStanding, Season


class Command(BaseCommand):
    help = "Collect PostgreSQL EXPLAIN (ANALYZE, BUFFERS) for approved query families"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument(
            "family",
            choices=(
                "reward-history",
                "network-activity",
                "ranking-hydration",
                "final-snapshot",
                "close-readiness",
            ),
        )
        parser.add_argument("--season", required=True)

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        season = Season.objects.get(code=options["season"])
        queries = {
            "reward-history": RewardLedger.objects.filter(season=season).order_by("-occurred_at")[
                :100
            ],
            "network-activity": NetworkActivity.objects.filter(season=season).order_by(
                "observed_at"
            )[:500],
            "ranking-hydration": RankingProjectionEvent.objects.filter(
                season=season, processed_at__isnull=True
            )[:500],
            "final-snapshot": PlayerSeasonFinalStanding.objects.filter(season=season).order_by(
                "final_rank"
            )[:500],
            "close-readiness": RewardLedger.objects.filter(season=season).values("player_id")[:500],
        }
        try:
            self.stdout.write(queries[options["family"]].explain(analyze=True, buffers=True))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
