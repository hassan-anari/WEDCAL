import json
from typing import Any

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.arena.alerts import alert_routes, detect_conditions, retry_dead_delivery, scan_alerts


class Command(BaseCommand):
    help = "Read-only economic alert preview unless --apply; never delivers external messages."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--retry-delivery", type=int)
        parser.add_argument("--actor")
        parser.add_argument("--reference")

    def handle(self, *args: Any, **options: Any) -> None:
        alert_routes()
        if options["retry_delivery"] is not None:
            report: Any = retry_dead_delivery(
                options["retry_delivery"],
                actor=User.objects.get(username=options["actor"]),
                reference=options["reference"] or "",
                dry_run=not options["apply"],
            )
        elif options["apply"]:
            report = {"detected": scan_alerts(), "delivery_requested": False}
        else:
            report = {"dry_run": True, "conditions": detect_conditions()}
        self.stdout.write(json.dumps(report, sort_keys=True))
