"""Bounded private view of canonical committed Clan records; no reward mutation."""

from datetime import datetime
from typing import Any

from django.core import signing
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.clans.models import (
    ClanEconomicSettlement,
    ClanItemPurchase,
    ClanItemUse,
    ClanMembership,
    ClanMembershipEvent,
    ClanMissionProgress,
    ClanTreasuryEntry,
)
from apps.core.b3 import B3Error

SALT = "clan-activity-v1"


def activity_page(membership: ClanMembership, *, limit: int, cursor: str | None) -> dict[str, Any]:
    if not 1 <= limit <= 50:
        raise B3Error("REQUEST_INVALID", 422)
    cs = membership.clan_season_id
    boundary: tuple[datetime, str] | None = None
    if cursor:
        if len(cursor) > 1024:
            raise B3Error("ACTIVITY_CURSOR_INVALID", 422)
        try:
            data = signing.loads(cursor, salt=SALT)
            if data["clan_season"] != cs or data["version"] != 1:
                raise ValueError
            boundary = (datetime.fromisoformat(data["at"]), data["id"])
            if timezone.is_naive(boundary[0]):
                raise ValueError
        except (signing.BadSignature, ValueError, KeyError, TypeError):
            raise B3Error("ACTIVITY_CURSOR_INVALID", 422) from None
    now = timezone.now()
    items = []

    def rows(query: Any, prefix: str, time_field: str) -> Any:
        query = query.filter(**{f"{time_field}__lte": now})
        if boundary:
            at, identity = boundary
            # Equal-time records use the same lexical namespaced identity as the merge.
            if identity.startswith(prefix + ":"):
                try:
                    pk = int(identity.split(":", 1)[1])
                except ValueError:
                    raise B3Error("ACTIVITY_CURSOR_INVALID", 422) from None
                equal = Q(**{time_field: at, "pk__lt": pk})
            else:
                equal = (
                    Q(**{time_field: at}) if prefix < identity.split(":", 1)[0] else Q(pk__in=[])
                )
            query = query.filter(Q(**{f"{time_field}__lt": at}) | equal)
        return query.order_by(f"-{time_field}", "-pk")[: limit + 1]

    def append(row: Any, prefix: str, kind: str, at: datetime, **values: Any) -> None:
        items.append(
            {
                "id": f"{prefix}:{row.pk:020d}",
                "event_type": kind,
                "occurred_at": at,
                "recorded_at": getattr(row, "created_at", None),
                **values,
            }
        )

    settlements = ClanEconomicSettlement.objects.filter(
        clan_season_id=cs, source_type="CLAN_ACTIVITY"
    ).select_related("membership__player")
    for row in rows(settlements, "reward", "occurred_at"):
        append(
            row,
            "reward",
            "CLAN_REWARD_SETTLED",
            row.occurred_at,
            actor_name=row.membership.player.first_name,
            amount=row.player_amount,
            unit="PLAYER_POINTS",
            treasury_amount=row.treasury_amount,
            contribution_amount=row.contribution_amount,
        )
    completed = ClanMissionProgress.objects.filter(
        clan_season_id=cs, completed_at__isnull=False
    ).select_related("definition", "membership__player")
    for row in rows(completed, "mission", "completed_at"):
        append(
            row,
            "mission",
            "MISSION_COMPLETED",
            row.completed_at,
            actor_name=row.membership.player.first_name if row.membership else None,
            mission_key=row.definition.key,
            mission_version=row.definition.version,
        )
    # Do not repeat the Treasury leg of a reward or purchase already represented above.
    related = ClanEconomicSettlement.objects.filter(
        clan_season_id=cs, source_type=OuterRef("source_type"), source_id=OuterRef("source_id")
    )
    treasury = (
        ClanTreasuryEntry.objects.filter(clan_season_id=cs, clanitempurchase__isnull=True)
        .annotate(related_reward=Exists(related))
        .filter(related_reward=False)
    )
    for row in rows(treasury, "treasury", "occurred_at"):
        append(
            row,
            "treasury",
            f"TREASURY_{row.direction}",
            row.occurred_at,
            amount=row.amount,
            unit="TREASURY_CREDITS",
        )
    purchases = ClanItemPurchase.objects.filter(clan_season_id=cs).select_related("purchaser")
    for row in rows(purchases, "purchase", "created_at"):
        append(
            row,
            "purchase",
            "ITEM_PURCHASED",
            row.created_at,
            actor_name=row.purchaser.first_name,
            amount=row.price,
            unit="TREASURY_CREDITS",
            item_id=row.item_id,
        )
    uses = ClanItemUse.objects.filter(clan_season_id=cs).select_related("actor")
    for row in rows(uses, "use", "used_at"):
        append(
            row,
            "use",
            "ITEM_ACTIVATED",
            row.used_at,
            actor_name=row.actor.first_name,
            item_id=row.item_id,
        )
    joins = ClanMembershipEvent.objects.filter(
        membership__clan_season_id=cs, kind="ACTIVATED"
    ).select_related("membership__player")
    for row in rows(joins, "join", "occurred_at"):
        append(
            row,
            "join",
            "MEMBER_JOINED",
            row.occurred_at,
            actor_name=row.membership.player.first_name,
        )
    items.sort(key=lambda item: (item["occurred_at"], item["id"]), reverse=True)
    page = items[:limit]
    next_cursor = None
    if len(items) > limit:
        last = page[-1]
        next_cursor = signing.dumps(
            {
                "version": 1,
                "clan_season": cs,
                "at": last["occurred_at"].isoformat(),
                "id": last["id"],
            },
            salt=SALT,
        )
    return {"items": page, "next_cursor": next_cursor, "as_of": now}
