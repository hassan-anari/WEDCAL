import json
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.arena.alert_delivery import (
    format_message,
    materialize_event,
    prepare_test_delivery,
    recipient_preview,
    review_delivery,
    send_test_delivery,
)
from apps.arena.alerts import detect_conditions, scan_alerts
from apps.arena.models import EconomicAlertEvent


class Command(BaseCommand):
    help = "Preview economic monitoring/routing. External tests require --apply --send-test."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--apply", action="store_true")
        operations = parser.add_mutually_exclusive_group()
        operations.add_argument("--retry-delivery", type=int)
        operations.add_argument("--resolve-delivery", type=int)
        operations.add_argument("--test-event", type=int)
        operations.add_argument("--route-event", type=int)
        parser.add_argument(
            "--resolution", choices=["CONFIRM_DELIVERED", "CONFIRM_NOT_SENT_AND_RETRY", "ABANDON"]
        )
        parser.add_argument("--message-id", type=int)
        parser.add_argument("--send-test", action="store_true")
        parser.add_argument("--actor")
        parser.add_argument("--reference")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            report = self.report(options)
        except (
            PermissionDenied,
            ValidationError,
            User.DoesNotExist,
            EconomicAlertEvent.DoesNotExist,
        ) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, sort_keys=True))

    def report(self, options: dict[str, Any]) -> Any:
        apply = options["apply"]
        if options["send_test"] and (not apply or options["test_event"] is None):
            raise CommandError(
                "--send-test requires --apply --test-event and separate send authorization"
            )
        if any(
            options[key] is not None
            for key in ("retry_delivery", "resolve_delivery", "test_event", "route_event")
        ):
            if not options["actor"] or not options["reference"]:
                raise CommandError("Explicit --actor and --reference required")
            actor = User.objects.get(username=options["actor"])
            reference = options["reference"]
            if options["test_event"] is not None:
                result = prepare_test_delivery(
                    options["test_event"], actor=actor, reference=reference, dry_run=not apply
                )
                if options["send_test"]:
                    result["delivered"] = [
                        send_test_delivery(pk, actor=actor, reference=reference)
                        for pk in result["delivery_ids"]
                    ]
                return result
            if options["route_event"] is not None:
                from apps.arena.alert_delivery import _operator_authority

                _operator_authority(actor, reference)
                event = EconomicAlertEvent.objects.select_related("alert__epoch").get(
                    pk=options["route_event"]
                )
                if event.outcome == "TEST_REQUESTED":
                    raise CommandError(
                        "Test events may only target their original authorized operator"
                    )
                result = {
                    "dry_run": not apply,
                    "message": format_message(event),
                    "recipients": recipient_preview(),
                }
                if apply:
                    from django.db import transaction

                    from apps.arena.policy import digest

                    with transaction.atomic():
                        EconomicAlertEvent.objects.create(
                            alert_id=event.alert_id,
                            outcome="ROUTING_RETRY",
                            severity=event.severity,
                            evidence={
                                "source_event_id": event.pk,
                                "actor_id": actor.pk,
                                "reference_digest": digest(reference),
                            },
                        )
                        result["delivery_ids"] = materialize_event(event)
                return result
            return review_delivery(
                options["retry_delivery"] or options["resolve_delivery"],
                actor=actor,
                reference=reference,
                action="RETRY_DEAD"
                if options["retry_delivery"] is not None
                else options["resolution"] or "",
                dry_run=not apply,
                message_id=options["message_id"],
            )
        if apply:
            return {"detected": scan_alerts(), "delivery_requested": False}
        return {
            "dry_run": True,
            "conditions": detect_conditions(),
            "recipients": recipient_preview(),
        }
