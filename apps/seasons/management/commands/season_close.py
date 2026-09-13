import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.seasons.close import preview_close, start_or_resume_close
from apps.seasons.models import Season


class Command(BaseCommand):
    help = "Preview or start/resume the durable Season-close workflow"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("season")
        parser.add_argument("--start", action="store_true")
        parser.add_argument("--actor")
        parser.add_argument("--reason")
        parser.add_argument("--ticket", default="")

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        season = Season.objects.get(code=options["season"])
        self.stdout.write(json.dumps(preview_close(season).as_dict(), indent=2, default=str))
        if not options["start"]:
            return
        if not options["actor"] or not options["reason"]:
            raise CommandError("--actor and --reason are required with --start")
        actor = get_user_model().objects.get(username=options["actor"])
        run = start_or_resume_close(
            season, actor=actor, reason=options["reason"], ticket_reference=options["ticket"]
        )
        self.stdout.write(self.style.SUCCESS(f"close_run={run.pk} stage={run.stage}"))
