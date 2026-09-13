import json
from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.clans.economy import canonical_clan_projection, rebuild_clan_projections
from apps.clans.models import ClanSeason
from apps.seasons.models import Season


def _json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


class Command(BaseCommand):
    help = "Bounded dry-run/rebuild of accepted Clan economy projections"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("season")
        parser.add_argument("--after-id", type=int, default=0)
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--repair", action="store_true")
        parser.add_argument("--actor")
        parser.add_argument("--reason")

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        season = Season.objects.get(code=options["season"])
        if options["repair"] and (not options["actor"] or not options["reason"]):
            raise CommandError("--actor and --reason are required for repair")
        limit = min(max(options["limit"], 1), 2000)
        rows = ClanSeason.objects.filter(season=season, pk__gt=options["after_id"]).order_by("pk")[
            :limit
        ]
        report = []
        for cs in rows:
            expected = canonical_clan_projection(cs)
            drift = {
                key: {"projected": _json_value(getattr(cs, key)), "expected": _json_value(value)}
                for key, value in expected.items()
                if getattr(cs, key) != value
            }
            if options["repair"] and drift:
                with transaction.atomic():
                    rebuilt = rebuild_clan_projections(cs)
                    from apps.notifications.models import OperatorAuditEvent

                    OperatorAuditEvent.objects.create(
                        actor=get_user_model().objects.get(username=options["actor"]),
                        action="RECONCILE_CLAN_ECONOMY",
                        target_type="ClanSeason",
                        target_id=str(cs.pk),
                        reason=options["reason"],
                        before_state={key: value["projected"] for key, value in drift.items()},
                        after_state={key: _json_value(getattr(rebuilt, key)) for key in expected},
                    )
            report.append(
                {
                    "clan_season_id": cs.pk,
                    "drift": drift,
                    "repaired": bool(options["repair"] and drift),
                }
            )
        self.stdout.write(json.dumps(report, indent=2))
