import json
from pathlib import Path
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.arena.policy_registration import approve_policy, propose_policy


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class Command(BaseCommand):
    help = "Validate/propose or independently approve/register a policy. Dry-run unless --apply."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("operation", choices=("propose", "approve"))
        parser.add_argument("--actor", required=True)
        parser.add_argument("--reference", required=True)
        parser.add_argument("--checksum")
        parser.add_argument("--file", type=Path)
        parser.add_argument("--key")
        parser.add_argument("--policy-version", dest="version", type=int)
        parser.add_argument("--proposal", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            actor = User.objects.get(username=options["actor"])
            common: dict[str, Any] = {"actor": actor, "dry_run": not options["apply"]}
            if options["operation"] == "propose":
                path = options["file"]
                if path is None or not options["key"] or options["version"] is None:
                    raise CommandError("propose requires --file, --key and --policy-version")
                if path.stat().st_size > 1024 * 1024:
                    raise CommandError("Policy input exceeds the 1 MiB safety bound")
                payload = json.loads(path.read_text(), object_pairs_hook=unique_object)
                result = propose_policy(
                    **common,
                    payload=payload,
                    key=options["key"],
                    version=options["version"],
                    submission_reference=options["reference"],
                    expected_checksum=options["checksum"],
                )
            else:
                if options["proposal"] is None or not options["checksum"]:
                    raise CommandError("approve requires --proposal and --checksum")
                result = approve_policy(
                    options["proposal"],
                    **common,
                    approval_reference=options["reference"],
                    expected_checksum=options["checksum"],
                )
        except (ValidationError, PermissionDenied, ValueError, OSError, ObjectDoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
