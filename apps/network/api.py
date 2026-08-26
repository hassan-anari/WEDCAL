from typing import Any

from django.http import HttpRequest
from ninja import Router

from apps.network.models import CreatorGroupRegistration
from apps.network.services import create_registration_intent
from apps.network.tasks import verify_creator_registration
from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["network"], auth=mini_app_auth)


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    if not isinstance(player, Player):
        raise RuntimeError("Mini App authentication did not resolve a player")
    return player


@router.get("/groups", response=dict[str, Any])
def groups(request: HttpRequest) -> dict[str, Any]:
    rows = (
        CreatorGroupRegistration.objects.filter(creator=_player(request))
        .select_related("group")
        .order_by("id")
    )
    return {
        "items": [
            {"public_id": str(x.public_id), "title": x.group.title, "state": x.state} for x in rows
        ]
    }


@router.post("/groups/intents", response=dict[str, Any])
def intent(request: HttpRequest) -> dict[str, Any]:
    _, link = create_registration_intent(_player(request))
    return {"startgroup_url": link}


@router.post("/groups/{public_id}/reverify", response={202: dict[str, bool], 404: dict[str, str]})
def reverify(request: HttpRequest, public_id: str) -> tuple[int, dict[str, str] | dict[str, bool]]:
    item = CreatorGroupRegistration.objects.filter(
        public_id=public_id,
        creator=_player(request),
        state__in=(
            CreatorGroupRegistration.State.REVIEW,
            CreatorGroupRegistration.State.ADMIN_REQUIRED,
            CreatorGroupRegistration.State.ACTIVE,
        ),
    ).first()
    if not item:
        return 404, {"detail": "Not found"}
    verify_creator_registration.delay(item.pk)
    return 202, {"accepted": True}
