import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.arena.models import EconomicEpoch
from apps.arena.readiness import preflight_epoch


class Command(BaseCommand):
    help = "Read-only Economic Epoch freeze readiness. Never evaluates, freezes or settles."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("season")
        parser.add_argument("epoch", type=int, choices=range(1, 7))

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            epoch = EconomicEpoch.objects.get(
                season__code=options["season"], epoch_number=options["epoch"]
            )
        except EconomicEpoch.DoesNotExist as exc:
            raise CommandError("Season/Epoch not found", returncode=3) from exc
        report = preflight_epoch(epoch)
        self.stdout.write(json.dumps(report, sort_keys=True))
        raise CommandError(
            "NOT EVALUATED" if report["readiness"] == "NOT_EVALUATED" else "BLOCKED",
            returncode=2,
        )
