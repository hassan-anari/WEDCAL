import sys

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.clans.qa_recreation import grant_qa_recreation
from apps.players.models import Player
from apps.seasons.models import Season
from apps.seasons.services import NoActiveSeason, SeasonNotEligible


class Command(BaseCommand):
    help = "Grant one audited, player/Season-scoped QA Clan recreation permission"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--actor", required=True)
        parser.add_argument("--season", required=True, help="Exact current Season code")
        parser.add_argument("--reason", required=True)
        parser.add_argument("--ticket-reference", required=True)
        parser.add_argument("--telegram-user-id-stdin", action="store_true", required=True)

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        try:
            raw = sys.stdin.readline(32).strip()
            if (
                not raw.isascii()
                or not raw.isdecimal()
                or len(raw) > 19
                or not 0 < int(raw) <= 2**63 - 1
            ):
                raise ValueError
            player = Player.objects.get(telegram_user_id=int(raw))
            grant, created = grant_qa_recreation(
                actor=get_user_model().objects.get(username=options["actor"]),
                player=player,
                season=Season.objects.get(code=options["season"]),
                reason=options["reason"],
                ticket_reference=options["ticket_reference"],
            )
        except (
            ValueError,
            Player.DoesNotExist,
            Season.DoesNotExist,
            get_user_model().DoesNotExist,
            ValidationError,
            PermissionDenied,
            NoActiveSeason,
            SeasonNotEligible,
        ) as exc:
            raise CommandError(
                "Grant rejected; verify operator, account, current Season and eligibility"
            ) from exc
        self.stdout.write(
            f"grant={grant.pk} created={created} consumed={grant.consumed_at is not None}"
        )
