from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.arena.anomalies import resolve_epoch_anomaly
from apps.arena.models import EpochAnomaly, EpochAnomalyResolution


class Command(BaseCommand):
    help = "Append an audited operator disposition for an economic epoch anomaly"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("anomaly_id", type=int)
        parser.add_argument(
            "--outcome", required=True, choices=EpochAnomalyResolution.Outcome.values
        )
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--evidence", default="")
        parser.add_argument("--idempotency-key", required=True)

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        resolution, created = resolve_epoch_anomaly(
            EpochAnomaly.objects.get(pk=options["anomaly_id"]),
            outcome=options["outcome"],
            actor=get_user_model().objects.get(username=options["actor"]),
            reason=options["reason"],
            evidence_reference=options["evidence"],
            idempotency_key=options["idempotency_key"],
        )
        self.stdout.write(f"resolution={resolution.pk} created={created}")
