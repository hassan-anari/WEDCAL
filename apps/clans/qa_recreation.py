"""Explicit administrative QA exception; normal Clan policy remains unchanged."""

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.clans.models import (
    ClanMembership,
    ClanRecreationGrant,
    ClanRole,
    ClanSeason,
    PlayerSeasonClanState,
)
from apps.notifications.models import OperatorAuditEvent
from apps.players.models import Player
from apps.seasons.barrier import acquire_economic_write_barrier
from apps.seasons.models import Season
from apps.seasons.services import resolve_current_season


def recreation_grant_available(player: Player, season: Season) -> bool:
    return ClanRecreationGrant.objects.filter(
        player=player, season=season, consumed_at__isnull=True
    ).exists()


@transaction.atomic
def grant_qa_recreation(
    *, actor: User, player: Player, season: Season, reason: str, ticket_reference: str
) -> tuple[ClanRecreationGrant, bool]:
    if not actor.is_active or not actor.has_perm("clans.grant_qa_clan_recreation"):
        raise PermissionDenied("QA recreation grant permission required")
    if (
        not reason.strip()
        or not ticket_reference.strip()
        or len(reason) > 1000
        or len(ticket_reference) > 128
    ):
        raise ValidationError("A bounded reason and owner approval reference are required")
    acquire_economic_write_barrier(season.pk)
    Player.objects.select_for_update().get(pk=player.pk)
    current = resolve_current_season(at=timezone.now())
    if current is None or current.pk != season.pk:
        raise ValidationError("QA recreation grant requires the current active Season")
    existing = ClanRecreationGrant.objects.filter(player=player, season=season).first()
    if existing:
        if (existing.granted_by_id, existing.reason, existing.ticket_reference) != (
            actor.pk,
            reason,
            ticket_reference,
        ):
            raise ValidationError(
                "This player's Season already has a QA grant; it cannot be reissued"
            )
        return existing, False
    if (
        not PlayerSeasonClanState.objects.filter(
            player=player, season=season, role=ClanRole.CAPTAIN
        ).exists()
        or not ClanSeason.objects.filter(
            captain=player, season=season, state=ClanSeason.State.ENDED
        ).exists()
    ):
        raise ValidationError("QA grant requires a Captain with an ended ClanSeason")
    if ClanMembership.objects.filter(player=player, ended_at__isnull=True).exists() or (
        ClanSeason.objects.filter(captain=player, season=season).exclude(state="ENDED").exists()
    ):
        raise ValidationError("QA grant requires no current membership or live Clan")
    grant = ClanRecreationGrant.objects.create(
        player=player,
        season=season,
        granted_by=actor,
        reason=reason,
        ticket_reference=ticket_reference,
    )
    OperatorAuditEvent.objects.create(
        actor=actor,
        action="QA_CLAN_RECREATION_GRANTED",
        target_type="ClanRecreationGrant",
        target_id=str(grant.pk),
        reason=reason,
        ticket_reference=ticket_reference,
        after_state={"player_id": player.pk, "season_id": season.pk, "consumed": False},
    )
    return grant, True


def consume_qa_recreation(grant: ClanRecreationGrant, clan: ClanSeason) -> None:
    """Called under the registration Player/grant locks, in its creation transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("QA grant consumption requires the registration transaction")
    if grant.consumed_at or (grant.player_id, grant.season_id) != (clan.captain_id, clan.season_id):
        raise ValidationError("QA grant cannot be consumed for this registration")
    grant.consumed_at, grant.consumed_clan_season = timezone.now(), clan
    grant.save(update_fields=("consumed_at", "consumed_clan_season"))
    OperatorAuditEvent.objects.create(
        actor=grant.granted_by,
        action="QA_CLAN_RECREATION_CONSUMED",
        target_type="ClanRecreationGrant",
        target_id=str(grant.pk),
        reason=grant.reason,
        ticket_reference=grant.ticket_reference,
        before_state={"consumed": False},
        after_state={
            "consumed": True,
            "player_id": clan.captain_id,
            "season_id": clan.season_id,
            "clan_season_id": clan.pk,
        },
    )
