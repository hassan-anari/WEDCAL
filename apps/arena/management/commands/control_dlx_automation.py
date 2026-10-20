from typing import Any

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.arena.automation import control_automation
from apps.seasons.models import Season


class Command(BaseCommand):
    help = (
        "Explicit audited per-Season automation activation/pause. Never binds or approves policy."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("season")
        parser.add_argument("--actor", required=True)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--enable", action="store_true")
        group.add_argument("--pause", action="store_true")
        parser.add_argument("--reason", required=True)
        parser.add_argument("--evidence", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        event = control_automation(
            Season.objects.get(code=options["season"]),
            enabled=options["enable"],
            actor=User.objects.get(username=options["actor"]),
            reason=options["reason"],
            evidence_reference=options["evidence"],
        )
        self.stdout.write(f"control={event.pk} enabled={event.enabled}")
