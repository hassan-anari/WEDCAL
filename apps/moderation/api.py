from django.core.exceptions import ValidationError
from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.moderation.services import create_report
from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["moderation"], auth=mini_app_auth)


class ReportIn(Schema):
    target_player_id: int
    category: str
    reason: str
    network_activity_id: int | None = None


class ReportOut(Schema):
    case_id: str
    status: str


@router.post("/reports", response=ReportOut)
def report(request: HttpRequest, payload: ReportIn) -> ReportOut:
    reporter = getattr(request, "auth", None)
    assert isinstance(reporter, Player)
    try:
        target = Player.objects.get(pk=payload.target_player_id)
        case = create_report(
            reporter=reporter,
            target=target,
            category=payload.category,
            reason=payload.reason,
            network_activity_id=payload.network_activity_id,
        )
    except Player.DoesNotExist as exc:
        raise HttpError(404, "Target not found") from exc
    except ValidationError as exc:
        raise HttpError(409, str(exc)) from exc
    return ReportOut(case_id=str(case.public_id), status=case.status)
