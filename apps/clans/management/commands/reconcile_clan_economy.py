import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from apps.clans.models import (
    ClanContributionEntry,
    ClanEconomicSettlement,
    ClanHQXPEntry,
    ClanSeason,
    ClanTreasuryEntry,
)
from apps.seasons.models import Season


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
            treasury = (
                ClanTreasuryEntry.objects.filter(clan_season=cs)
                .values("direction")
                .annotate(total=Sum("amount"))
            )
            values = {row["direction"]: int(row["total"] or 0) for row in treasury}
            expected = {
                "treasury_balance": values.get("CREDIT", 0) - values.get("DEBIT", 0),
                "treasury_earned_total": values.get("CREDIT", 0),
                "contribution_total": int(
                    ClanContributionEntry.objects.filter(clan_season=cs).aggregate(
                        value=Sum("amount")
                    )["value"]
                    or 0
                ),
                "hq_xp": int(
                    ClanHQXPEntry.objects.filter(clan_season=cs).aggregate(value=Sum("amount"))[
                        "value"
                    ]
                    or 0
                ),
                "competitive_score": int(
                    ClanEconomicSettlement.objects.filter(clan_season=cs).aggregate(
                        value=Sum("clan_score_amount")
                    )["value"]
                    or 0
                ),
            }
            drift = {
                key: {"projected": getattr(cs, key), "expected": value}
                for key, value in expected.items()
                if getattr(cs, key) != value
            }
            if options["repair"] and drift:
                with transaction.atomic():
                    ClanSeason.objects.select_for_update().filter(pk=cs.pk).update(**expected)
                    from apps.notifications.models import OperatorAuditEvent

                    OperatorAuditEvent.objects.create(
                        actor=get_user_model().objects.get(username=options["actor"]),
                        action="RECONCILE_CLAN_ECONOMY",
                        target_type="ClanSeason",
                        target_id=str(cs.pk),
                        reason=options["reason"],
                        before_state={key: value["projected"] for key, value in drift.items()},
                        after_state=expected,
                    )
            report.append(
                {
                    "clan_season_id": cs.pk,
                    "drift": drift,
                    "repaired": bool(options["repair"] and drift),
                }
            )
        self.stdout.write(json.dumps(report, indent=2))
