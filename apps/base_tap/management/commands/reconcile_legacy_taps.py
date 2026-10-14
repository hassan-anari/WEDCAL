"""Read-only by default. Execute only against an explicitly approved database."""

import json
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Sum
from django.utils import timezone

from apps.arena.models import PerformanceSourceFact
from apps.base_tap.models import BaseTapDailyAccumulator, BaseTapReceipt
from apps.base_tap.services import finalize_tap_accumulator
from apps.rewards.models import RewardLedger


class Command(BaseCommand):
    help = (
        "Audit legacy Tap obligations; optionally drain a bounded batch without rewriting history"
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--after-id", type=int, default=0)

    def handle(self, *args: Any, **options: Any) -> None:
        if not 1 <= options["limit"] <= 10000:
            raise CommandError("limit must be 1..10000")
        if options["apply"] and settings.BASE_TAP_LEGACY_INGRESS_ENABLED:
            raise CommandError("Pause legacy ingress and stop old-version workers before drain")
        rows = BaseTapDailyAccumulator.objects.filter(pk__gt=options["after_id"]).order_by("pk")[
            : options["limit"]
        ]
        report: list[dict[str, Any]] = []
        for row in rows:
            key = f"base-tap-day:{row.pk}"
            ledger = RewardLedger.objects.filter(idempotency_key=key).first()
            # Source facts survive receipt deletion and keep the accepted source-time XP.
            fact_total = (
                PerformanceSourceFact.objects.filter(
                    player_id=row.player_id,
                    season_id=row.season_id,
                    source_type="BASE_TAP_BATCH",
                    metadata__utc_day=str(row.utc_day),
                ).aggregate(total=Sum("amount"))["total"]
                or 0
            )
            obligation = row.pending_points + row.settled_points
            consistent = fact_total == obligation and (
                ledger is None or ledger.player_amount == obligation
            )
            if row.settlement_state == "SETTLED":
                consistent = consistent and ledger is not None and row.pending_points == 0
            before = {
                "accumulator_id": row.pk,
                "player_id": row.player_id,
                "season_id": row.season_id,
                "config_id": row.config_version_id,
                "accepted_taps": row.accepted_taps,
                "pending_points": row.pending_points,
                "settled_points": row.settled_points,
                "state": row.settlement_state,
                "ledger_id": ledger.pk if ledger else None,
                "source_fact_points": fact_total,
                "receipt_count": BaseTapReceipt.objects.filter(accumulator=row).count(),
                "consistent": consistent,
            }
            if options["apply"] and row.pending_points:
                if not consistent:
                    raise CommandError(f"Accumulator {row.pk} requires independent reconciliation")
                after = finalize_tap_accumulator(row.pk, at=timezone.now(), force=True)
                before["after_state"] = after.settlement_state
                before["after_pending"] = after.pending_points
            report.append(before)
        self.stdout.write(
            json.dumps(
                {
                    "mode": "apply" if options["apply"] else "dry-run",
                    "observed_at": timezone.now().isoformat(),
                    "rows": report,
                    "next_after_id": report[-1]["accumulator_id"]
                    if report
                    else options["after_id"],
                },
                sort_keys=True,
            )
        )
