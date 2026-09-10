from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.clans.models import (
    Clan,
    ClanInvite,
    ClanJoinRequest,
    ClanMembership,
    ClanMembershipEvent,
    ClanRole,
    ClanSeason,
    PlayerSeasonClanState,
)
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ClanConfig, ResolvedGameConfig, resolve_game_config_version
from apps.players.models import Player
from apps.progression.services import refresh_player_progression
from apps.seasons.models import Season
from apps.telegram import bot_api
from apps.telegram.models import TelegramGroup
from apps.telegram.services import membership_semantics


class ClanConflict(ValidationError):
    pass


def require_policy(resolved: ResolvedGameConfig) -> ClanConfig:
    if resolved.clan is None or not resolved.clan.enabled:
        raise ClanConflict("Clan feature is unavailable")
    return resolved.clan


def require_eligible(player: Player, resolved: ResolvedGameConfig) -> ClanConfig:
    policy = require_policy(resolved)
    _, progress = refresh_player_progression(player, resolved)
    if progress.level < policy.min_level:
        raise ClanConflict("Clan progression gate is not met")
    return policy


@transaction.atomic
def choose_role(player: Player, season: Season, role: str) -> PlayerSeasonClanState:
    if role not in ClanRole.values:
        raise ClanConflict("Role must be CAPTAIN or SOLDIER")
    Player.objects.select_for_update().get(pk=player.pk)
    state, created = PlayerSeasonClanState.objects.get_or_create(
        player=player, season=season, defaults={"role": role}
    )
    if not created and state.role != role:
        raise ClanConflict("Season Clan role is immutable")
    return state


def eligible_groups(
    player: Player, season: Season, resolved: ResolvedGameConfig
) -> QuerySet[TelegramGroup]:
    require_eligible(player, resolved)
    if not PlayerSeasonClanState.objects.filter(
        player=player, season=season, role=ClanRole.CAPTAIN
    ).exists():
        raise PermissionDenied("Captain role required")
    return (
        TelegramGroup.objects.filter(
            memberships__player=player,
            memberships__is_admin=True,
            bot_is_admin=True,
            bot_can_invite_users=True,
            title__gt="",
        )
        .exclude(
            clan__seasons__season=season,
            clan__seasons__state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
        )
        .distinct()
    )


def _fresh_authority(player: Player, group: TelegramGroup) -> None:
    user = bot_api.get_chat_member(group.telegram_chat_id, player.telegram_user_id)
    bot_id = settings.TELEGRAM_BOT_ID
    bot = bot_api.get_chat_member(group.telegram_chat_id, bot_id) if bot_id is not None else None
    if not user.ok or bot is None or not bot.ok:
        raise ClanConflict("Telegram authority verification is temporarily unavailable")
    assert user.result is not None and bot.result is not None
    if not membership_semantics(str(user.result.get("status", "")), user.result.get("is_member"))[
        1
    ]:
        raise PermissionDenied("Current Telegram admin authority required")
    if str(bot.result.get("status")) != "administrator" or not bot.result.get("can_invite_users"):
        raise ClanConflict("Bot invite capability required")


def register_clan(
    player: Player,
    season: Season,
    resolved: ResolvedGameConfig,
    *,
    group_id: int,
    name: str,
    tag: str,
    motto: str,
) -> ClanSeason:
    policy = require_eligible(player, resolved)
    group = eligible_groups(player, season, resolved).filter(pk=group_id).first()
    if group is None:
        raise PermissionDenied("Telegram group is not eligible")
    _fresh_authority(player, group)  # external I/O deliberately precedes locks
    normalized = tag.strip().casefold()
    if not (2 <= len(name.strip()) <= 64 and 2 <= len(normalized) <= 12 and len(motto) <= 160):
        raise ClanConflict("Invalid Clan identity")
    config = GameConfigVersion.objects.get(pk=resolved.id)
    with transaction.atomic():
        Player.objects.select_for_update().get(pk=player.pk)
        group = TelegramGroup.objects.select_for_update().get(pk=group.pk)
        existing = ClanSeason.objects.filter(
            captain=player,
            season=season,
            state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
        ).first()
        if existing:
            if existing.clan.telegram_group_id == group.pk:
                return existing
            raise ClanConflict("Captain already has a Clan this Season")
        if ClanMembership.objects.filter(
            player=player, season=season, ended_at__isnull=True
        ).exists():
            raise ClanConflict("Player already has a current Clan")
        clan, created = Clan.objects.get_or_create(
            telegram_group=group,
            defaults={
                "name": name.strip(),
                "tag": tag.strip(),
                "normalized_tag": normalized,
                "motto": motto,
                "creator": player,
            },
        )
        if not created:
            raise ClanConflict("Telegram group is already bound")
        clan_season = ClanSeason.objects.create(
            clan=clan,
            season=season,
            captain=player,
            base_capacity=policy.base_capacity,
            config_version=config,
        )
        membership = ClanMembership.objects.create(
            player=player,
            season=season,
            clan_season=clan_season,
            role=ClanRole.CAPTAIN,
            started_at=timezone.now(),
            config_version=config,
        )
        ClanMembershipEvent.objects.create(
            membership=membership, kind="ACTIVATED", occurred_at=membership.started_at
        )
        return clan_season


@transaction.atomic
def request_join(
    player: Player, clan_season: ClanSeason, resolved: ResolvedGameConfig
) -> ClanJoinRequest:
    policy = require_eligible(player, resolved)
    state = (
        PlayerSeasonClanState.objects.select_for_update()
        .filter(player=player, season=clan_season.season)
        .first()
    )
    if state is None or state.role != ClanRole.SOLDIER:
        raise PermissionDenied("Soldier role required")
    if state.leave_cooldown_until and state.leave_cooldown_until > timezone.now():
        raise ClanConflict("Clan leave cooldown is active")
    if state.voluntary_leave_count >= policy.max_voluntary_leaves_per_season:
        raise ClanConflict("Season voluntary leave limit reached")
    if ClanMembership.objects.filter(
        player=player, season=clan_season.season, ended_at__isnull=True
    ).exists():
        raise ClanConflict("Player already has a current Clan")
    if clan_season.state != ClanSeason.State.ACTIVE or not clan_season.accepting_members:
        raise ClanConflict("Clan is not accepting members")
    request = ClanJoinRequest.objects.filter(
        clan_season=clan_season,
        applicant=player,
        state__in=[ClanJoinRequest.State.PENDING, ClanJoinRequest.State.ACCEPTED],
    ).first()
    if request is not None:
        return request
    try:
        request = ClanJoinRequest.objects.create(
            clan_season=clan_season, applicant=player, state=ClanJoinRequest.State.PENDING
        )
    except IntegrityError as exc:
        raise ClanConflict("A live request already exists") from exc
    return request


@transaction.atomic
def decide_request(captain: Player, request_id: int, *, accept: bool) -> ClanJoinRequest:
    req = (
        ClanJoinRequest.objects.select_for_update().select_related("clan_season").get(pk=request_id)
    )
    ClanSeason.objects.select_for_update().get(pk=req.clan_season_id)
    Player.objects.select_for_update().get(pk=req.applicant_id)
    if req.clan_season.captain_id != captain.pk:
        raise PermissionDenied("Only this Clan's Captain may review applicants")
    if req.state != ClanJoinRequest.State.PENDING:
        return req
    if accept:
        occupied = req.clan_season.memberships.filter(ended_at__isnull=True).count()
        reserved = req.clan_season.join_requests.filter(
            state=ClanJoinRequest.State.ACCEPTED
        ).count()
        if occupied + reserved >= req.clan_season.base_capacity:
            raise ClanConflict("Clan capacity is full")
        if ClanJoinRequest.objects.filter(
            applicant=req.applicant, state=ClanJoinRequest.State.ACCEPTED
        ).exists():
            raise ClanConflict("Applicant already has an accepted path")
        req.state = ClanJoinRequest.State.ACCEPTED
        ClanJoinRequest.objects.filter(
            applicant=req.applicant, state=ClanJoinRequest.State.PENDING
        ).exclude(pk=req.pk).update(state=ClanJoinRequest.State.CANCELLED)
        ClanInvite.objects.create(join_request=req)
    else:
        req.state = ClanJoinRequest.State.DECLINED
    req.decided_at = timezone.now()
    req.save(update_fields=("state", "decided_at"))
    return req


@transaction.atomic
def cancel_request(player: Player, request_id: int) -> ClanJoinRequest:
    req = ClanJoinRequest.objects.select_for_update().get(pk=request_id, applicant=player)
    if req.state == ClanJoinRequest.State.PENDING:
        req.state = ClanJoinRequest.State.CANCELLED
        req.save(update_fields=("state",))
    elif req.state == ClanJoinRequest.State.ACCEPTED:
        raise ClanConflict("Accepted path must be resolved before cancellation")
    return req


def create_personal_invite(invite: ClanInvite) -> ClanInvite:
    invite = ClanInvite.objects.select_related(
        "join_request__clan_season__clan__telegram_group", "join_request__applicant"
    ).get(pk=invite.pk)
    if invite.state in {ClanInvite.State.ACTIVE, ClanInvite.State.UNCERTAIN, ClanInvite.State.USED}:
        return invite
    name = f"dexo-{invite.pk}-{invite.join_request.applicant_id}"
    ClanInvite.objects.filter(pk=invite.pk).update(
        state=ClanInvite.State.CREATING,
        attempt_count=models.F("attempt_count") + 1,
        telegram_invite_name=name,
    )
    result = bot_api.create_chat_invite_link(
        invite.join_request.clan_season.clan.telegram_group.telegram_chat_id, name
    )
    if result.ok and result.result and isinstance(result.result.get("invite_link"), str):
        link = result.result["invite_link"]
        ClanInvite.objects.filter(pk=invite.pk).update(
            state=ClanInvite.State.ACTIVE, invite_link=link, last_error_category=""
        )
        dm = bot_api.send_message(
            invite.join_request.applicant.telegram_user_id, f"Your Dexo Clan invite: {link}"
        )
        ClanInvite.objects.filter(pk=invite.pk).update(
            delivery_state=ClanInvite.Delivery.SENT if dm.ok else ClanInvite.Delivery.FAILED,
            delivery_error_category="" if dm.ok else (dm.category or "unknown"),
        )
    else:
        ClanInvite.objects.filter(pk=invite.pk).update(
            state=ClanInvite.State.UNCERTAIN if result.ambiguous else ClanInvite.State.FAILED,
            last_error_category=result.category or "unknown",
        )
    return ClanInvite.objects.get(pk=invite.pk)


@transaction.atomic
def reconcile_telegram_membership(
    *,
    player: Player,
    group: TelegramGroup,
    present: bool,
    telegram_update_id: int,
    observed_at: datetime,
) -> None:
    membership = (
        ClanMembership.objects.select_for_update()
        .filter(player=player, clan_season__clan__telegram_group=group, ended_at__isnull=True)
        .first()
    )
    if membership:
        if telegram_update_id <= membership.last_telegram_update_id:
            return
        policy = resolve_game_config_version(membership.config_version_id).clan
        assert policy is not None
        membership.last_telegram_update_id = telegram_update_id
        if present:
            membership.access_state = ClanMembership.Access.ACTIVE
            membership.access_lost_at = membership.access_rejoin_deadline = None
            kind = "ACCESS_RESTORED"
        else:
            membership.access_state = ClanMembership.Access.ACCESS_LOST
            membership.access_lost_at = observed_at
            membership.access_rejoin_deadline = observed_at + timedelta(
                hours=policy.access_rejoin_grace_hours
            )
            kind = "ACCESS_LOST"
        membership.save()
        ClanMembershipEvent.objects.get_or_create(
            membership=membership,
            kind=kind,
            telegram_update_id=telegram_update_id,
            defaults={"occurred_at": observed_at},
        )
        return
    if not present:
        return
    req = (
        ClanJoinRequest.objects.select_for_update()
        .filter(
            applicant=player,
            clan_season__clan__telegram_group=group,
            state=ClanJoinRequest.State.ACCEPTED,
        )
        .select_related("clan_season")
        .first()
    )
    if (
        req is None
        or ClanMembership.objects.filter(
            player=player, season=req.clan_season.season, ended_at__isnull=True
        ).exists()
    ):
        return
    config = req.clan_season.config_version
    membership = ClanMembership.objects.create(
        player=player,
        season=req.clan_season.season,
        clan_season=req.clan_season,
        role=ClanRole.SOLDIER,
        started_at=observed_at,
        last_telegram_update_id=telegram_update_id,
        config_version=config,
    )
    req.state = ClanJoinRequest.State.ACTIVATED
    req.save(update_fields=("state",))
    ClanInvite.objects.filter(join_request=req).update(state=ClanInvite.State.USED)
    ClanMembershipEvent.objects.create(
        membership=membership,
        kind="ACTIVATED",
        telegram_update_id=telegram_update_id,
        occurred_at=observed_at,
    )


@transaction.atomic
def voluntary_leave(
    player: Player, season: Season, resolved: ResolvedGameConfig
) -> ClanMembership | None:
    policy = require_policy(resolved)
    Player.objects.select_for_update().get(pk=player.pk)
    state = PlayerSeasonClanState.objects.select_for_update().get(player=player, season=season)
    membership = (
        ClanMembership.objects.select_for_update()
        .filter(player=player, season=season, ended_at__isnull=True)
        .first()
    )
    if membership is None:
        return None
    if membership.role != ClanRole.SOLDIER:
        raise PermissionDenied("Captain cannot use voluntary leave")
    if state.voluntary_leave_count >= policy.max_voluntary_leaves_per_season:
        raise ClanConflict("Season voluntary leave limit reached")
    now = timezone.now()
    membership.ended_at, membership.end_reason = now, ClanMembership.EndReason.VOLUNTARY_LEAVE
    membership.save(update_fields=("ended_at", "end_reason"))
    state.voluntary_leave_count += 1
    state.leave_cooldown_until = now + timedelta(hours=policy.leave_cooldown_hours)
    state.save(update_fields=("voluntary_leave_count", "leave_cooldown_until", "updated_at"))
    ClanMembershipEvent.objects.create(
        membership=membership, kind="VOLUNTARY_LEAVE", occurred_at=now
    )
    ClanJoinRequest.objects.filter(
        applicant=player, state__in=[ClanJoinRequest.State.PENDING, ClanJoinRequest.State.ACCEPTED]
    ).update(state=ClanJoinRequest.State.CANCELLED)
    ClanInvite.objects.filter(
        join_request__applicant=player,
        state__in=[ClanInvite.State.CREATING, ClanInvite.State.ACTIVE, ClanInvite.State.FAILED],
    ).update(state=ClanInvite.State.REVOKED, invite_link="")
    return membership
