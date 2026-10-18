from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.arena.models import RankingProjectionEvent
from apps.arena.services import apply_projection_event, live_clan_population, rebuild_ranking
from apps.notifications.models import OperatorAuditEvent
from apps.seasons.barrier import acquire_economic_write_barrier
from apps.seasons.models import Season


class Command(BaseCommand):
    help = "Rebuild an active Season Clan ranking without changing scores/history"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--season", required=True)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)

    @transaction.atomic
    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        actor = get_user_model().objects.get(username=options["actor"])
        if not actor.is_active or not actor.has_perm("arena.change_rankingprojectionstate"):
            raise CommandError("Ranking reconciliation permission required")
        if not options["reason"].strip():
            raise CommandError("A reconciliation reason is required")
        season = Season.objects.get(code=options["season"])
        acquire_economic_write_barrier(season.pk)
        season.refresh_from_db()
        now = timezone.now()
        if season.status != Season.Status.ACTIVE or not season.starts_at <= now < season.ends_at:
            raise CommandError(
                "This command only reconciles an active Season; historical ranks are retained"
            )
        if not rebuild_ranking(season, "CLAN"):
            raise CommandError(
                "Rebuild lock unavailable or expired; retry without deleting Redis keys"
            )
        # Replay old pending events through the lifecycle guard, then acknowledge them.
        for event in (
            RankingProjectionEvent.objects.filter(
                season=season, entity_type="CLAN", processed_at__isnull=True
            )
            .order_by("pk")
            .iterator(chunk_size=500)
        ):
            apply_projection_event(event)
        population = live_clan_population(season)
        OperatorAuditEvent.objects.create(
            actor=actor,
            action="REBUILD_LIVE_CLAN_RANKINGS",
            target_type="Season",
            target_id=str(season.pk),
            reason=options["reason"],
            after_state={"entity": "CLAN", "eligible_population": population},
        )
        self.stdout.write(f"rebuilt=True eligible_population={population}")
