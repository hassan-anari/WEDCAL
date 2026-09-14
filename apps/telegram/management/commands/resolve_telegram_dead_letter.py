from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.telegram.models import TelegramDeadLetterDisposition
from apps.telegram.services import resolve_dead_letter


class Command(BaseCommand):
    help = "Auditably requeue or dispose one Telegram DEAD_LETTER ingress row"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("inbound_id", type=int)
        parser.add_argument("outcome", choices=TelegramDeadLetterDisposition.Outcome.values)
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--evidence", required=True)
        parser.add_argument("--idempotency-key", required=True)

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        try:
            actor = get_user_model().objects.get(username=options["actor"])
        except get_user_model().DoesNotExist as exc:
            raise CommandError("Unknown actor") from exc
        disposition = resolve_dead_letter(
            options["inbound_id"],
            outcome=options["outcome"],
            actor=actor,
            reason=options["reason"],
            evidence_reference=options["evidence"],
            idempotency_key=options["idempotency_key"],
        )
        self.stdout.write(str(disposition.pk))
