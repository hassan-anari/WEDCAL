from contextlib import suppress
from typing import Annotated, Any
from uuid import UUID

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema
from pydantic import Field

from apps.core.b3 import B3Error, b3_responses
from apps.network.models import CreatorGroupRegistration
from apps.network.read_models import group_output, network_summary
from apps.network.schemas import CreatorGroupPage, IntentOutput, NetworkSummary
from apps.network.services import create_registration_intent
from apps.network.tasks import refresh_due_owned_groups, verify_creator_registration
from apps.players.models import Player
from apps.telegram.mini_app_auth import mini_app_auth

router = Router(tags=["network"], auth=mini_app_auth)


def _player(request: HttpRequest) -> Player:
    player = getattr(request, "auth", None)
    if not isinstance(player, Player):
        raise RuntimeError("Mini App authentication did not resolve a player")
    return player


@router.get("", response=b3_responses(NetworkSummary))
def summary(request: HttpRequest) -> dict[str, Any]:
    return network_summary(_player(request))


@router.get("/groups", response=b3_responses(CreatorGroupPage))
def groups(
    request: HttpRequest,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    refresh_due_owned_groups(_player(request).pk)
    rows = (
        CreatorGroupRegistration.objects.filter(creator=_player(request))
        .select_related("group")
        .order_by("id")
    )
    batch = list(rows[offset : offset + limit + 1])
    return {
        "items": [group_output(row) for row in batch[:limit]],
        "next_offset": offset + limit if len(batch) > limit else None,
    }


class IntentInput(Schema):
    authorize_group_messages: bool = Field(
        default=False,
        strict=True,
        description="Legacy preference metadata; never required for operator broadcasts.",
    )


@router.post("/groups/intents", response=b3_responses(IntentOutput))
def intent(request: HttpRequest, payload: IntentInput | None = None) -> dict[str, Any]:
    if not network_summary(_player(request))["can_register"]:
        raise B3Error("CREATOR_UNAVAILABLE")
    from django.conf import settings

    if not settings.TELEGRAM_BOT_USERNAME:
        raise B3Error("BOT_UNCONFIGURED", 503)
    row, link = create_registration_intent(
        _player(request),
        authorize_group_messages=payload.authorize_group_messages if payload else False,
    )
    return {"startgroup_url": link, "expires_at": row.expires_at}


@router.post("/groups/{public_id}/reverify", response={202: dict[str, bool], 404: dict[str, str]})
def reverify(request: HttpRequest, public_id: UUID) -> tuple[int, dict[str, str] | dict[str, bool]]:
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
    CreatorGroupRegistration.objects.filter(pk=item.pk).update(next_verification_at=timezone.now())
    # The periodic durable scanner recovers if broker dispatch fails.
    with suppress(Exception):
        verify_creator_registration.delay(item.pk)
    return 202, {"accepted": True}


class BroadcastConsentOutput(Schema):
    enabled: bool
    revision: int


class BroadcastConsentInput(Schema):
    enabled: bool = Field(strict=True)


@router.post(
    "/groups/{public_id}/broadcast-consent",
    response=b3_responses(BroadcastConsentOutput),
    deprecated=True,
)
def broadcast_consent(
    request: HttpRequest, public_id: UUID, payload: BroadcastConsentInput
) -> dict[str, Any]:
    from django.core.exceptions import ValidationError

    from apps.notifications.audiences import set_group_consent

    registration = (
        CreatorGroupRegistration.objects.select_related("group")
        .filter(public_id=public_id, creator=_player(request))
        .first()
    )
    if not registration:
        raise B3Error("GROUP_NOT_FOUND", 404)
    try:
        row = set_group_consent(
            registration.group, _player(request).telegram_user_id, enabled=payload.enabled
        )
    except ValidationError as exc:
        raise B3Error("GROUP_AUTHORIZATION_UNAVAILABLE", 409) from exc
    return {"enabled": row.enabled, "revision": row.revision}
