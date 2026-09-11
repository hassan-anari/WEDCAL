import uuid
from collections.abc import Callable
from contextlib import suppress
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
    existing = (
        ClanSeason.objects.filter(
            captain=player,
            season=season,
            state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
        )
        .select_related("clan")
        .first()
    )
    if existing is not None:
        if existing.clan.telegram_group_id == group_id:
            return existing
        raise ClanConflict("Captain already has a Clan this Season")
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
        try:
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
        except IntegrityError as exc:
            raise ClanConflict("Clan registration conflicts with an existing group or tag") from exc
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
    Player.objects.select_for_update().get(pk=player.pk)
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


def decide_request(captain: Player, request_id: int, *, accept: bool) -> ClanJoinRequest:
    identity = ClanJoinRequest.objects.values("applicant_id", "clan_season_id").get(pk=request_id)
    with transaction.atomic():
        # Global order: applicant Player -> Season state -> ClanSeason -> request/membership.
        Player.objects.select_for_update().get(pk=identity["applicant_id"])
        preview = ClanJoinRequest.objects.select_related("clan_season").get(pk=request_id)
        state = (
            PlayerSeasonClanState.objects.select_for_update()
            .filter(player_id=identity["applicant_id"], season=preview.clan_season.season)
            .first()
        )
        clan_season = ClanSeason.objects.select_for_update().get(pk=identity["clan_season_id"])
        req = (
            ClanJoinRequest.objects.select_for_update()
            .select_related("applicant")
            .get(pk=request_id)
        )
        if clan_season.captain_id != captain.pk:
            raise PermissionDenied("Only this Clan's Captain may review applicants")
        if req.state != ClanJoinRequest.State.PENDING:
            return req
        if not accept:
            req.state = ClanJoinRequest.State.DECLINED
        else:
            policy = resolve_game_config_version(clan_season.config_version_id).clan
            if (
                policy is None
                or clan_season.state != ClanSeason.State.ACTIVE
                or not clan_season.accepting_members
            ):
                raise ClanConflict("Clan is no longer accepting members")
            if state is None or state.role != ClanRole.SOLDIER:
                raise ClanConflict("Applicant no longer has the Soldier role")
            now = timezone.now()
            if state.leave_cooldown_until and state.leave_cooldown_until > now:
                raise ClanConflict("Applicant leave cooldown is active")
            if state.voluntary_leave_count >= policy.max_voluntary_leaves_per_season:
                raise ClanConflict("Applicant reached the Season leave limit")
            if ClanMembership.objects.filter(
                player_id=identity["applicant_id"], season=clan_season.season, ended_at__isnull=True
            ).exists():
                raise ClanConflict("Applicant already has a current Clan")
            if (
                ClanJoinRequest.objects.filter(
                    applicant_id=identity["applicant_id"], state=ClanJoinRequest.State.ACCEPTED
                )
                .exclude(pk=req.pk)
                .exists()
            ):
                raise ClanConflict("Applicant already has an accepted path")
            occupied = clan_season.memberships.filter(ended_at__isnull=True).count()
            reserved = clan_season.join_requests.filter(
                state=ClanJoinRequest.State.ACCEPTED
            ).count()
            if occupied + reserved >= clan_season.base_capacity:
                raise ClanConflict("Clan capacity is full")
            req.state = ClanJoinRequest.State.ACCEPTED
            ClanJoinRequest.objects.filter(
                applicant_id=identity["applicant_id"], state=ClanJoinRequest.State.PENDING
            ).exclude(pk=req.pk).update(state=ClanJoinRequest.State.CANCELLED)
            ClanInvite.objects.create(join_request=req, purpose=ClanInvite.Purpose.JOIN)
        req.decided_at = timezone.now()
        req.save(update_fields=("state", "decided_at"))
        return req


def cancel_request(player: Player, request_id: int) -> ClanJoinRequest:
    with transaction.atomic():
        Player.objects.select_for_update().get(pk=player.pk)
        req = ClanJoinRequest.objects.select_for_update().get(pk=request_id, applicant=player)
        if req.state == ClanJoinRequest.State.PENDING:
            req.state = ClanJoinRequest.State.CANCELLED
            req.save(update_fields=("state",))
        elif req.state == ClanJoinRequest.State.ACCEPTED:
            raise ClanConflict("Accepted path must be resolved before cancellation")
        return req


def _invite_target(invite: ClanInvite) -> tuple[Player, TelegramGroup]:
    if invite.purpose == ClanInvite.Purpose.JOIN:
        assert invite.join_request is not None
        return invite.join_request.applicant, invite.join_request.clan_season.clan.telegram_group
    assert invite.membership is not None
    return invite.membership.player, invite.membership.clan_season.clan.telegram_group


def create_personal_invite(invite: ClanInvite) -> ClanInvite:
    """Claim in PostgreSQL, call Telegram after commit, then reconcile by token."""
    now = timezone.now()
    with transaction.atomic():
        locked = ClanInvite.objects.select_for_update().get(pk=invite.pk)
        if locked.state in {
            ClanInvite.State.ACTIVE,
            ClanInvite.State.USED,
            ClanInvite.State.REVOKED,
            ClanInvite.State.REVOKE_PENDING,
            ClanInvite.State.REVOKE_FAILED,
        }:
            return locked
        if locked.state == ClanInvite.State.UNCERTAIN:
            return locked
        if locked.state == ClanInvite.State.CREATING:
            if locked.claim_expires_at and locked.claim_expires_at > now:
                return locked
            # A vanished caller may have succeeded remotely; never create another credential.
            locked.state = ClanInvite.State.UNCERTAIN
            locked.uncertain_since = now
            locked.claim_token = None
            locked.claim_expires_at = None
            locked.last_error_category = "create_lease_expired"
            locked.save()
            return locked
        if locked.attempt_count >= settings.CLAN_INVITE_MAX_CREATE_ATTEMPTS:
            locked.state = ClanInvite.State.UNCERTAIN
            locked.uncertain_since = now
            locked.last_error_category = "attempt_limit"
            locked.save()
            return locked
        token = uuid.uuid4()
        locked.state = ClanInvite.State.CREATING
        locked.claim_token = token
        locked.claim_expires_at = now + timedelta(seconds=settings.CLAN_INVITE_CREATE_LEASE_SECONDS)
        locked.attempt_count += 1
        locked.telegram_invite_name = f"dexo-{locked.pk}-{locked.attempt_count}"
        locked.save()
    invite = ClanInvite.objects.select_related(
        "join_request__applicant",
        "join_request__clan_season__clan__telegram_group",
        "membership__player",
        "membership__clan_season__clan__telegram_group",
    ).get(pk=invite.pk)
    target, group = _invite_target(invite)
    result = bot_api.create_chat_invite_link(group.telegram_chat_id, invite.telegram_invite_name)
    with transaction.atomic():
        locked = ClanInvite.objects.select_for_update().get(pk=invite.pk)
        if locked.state != ClanInvite.State.CREATING or locked.claim_token != token:
            return locked
        locked.claim_token = None
        locked.claim_expires_at = None
        if result.ok and result.result and isinstance(result.result.get("invite_link"), str):
            locked.state = ClanInvite.State.ACTIVE
            locked.invite_link = result.result["invite_link"]
            locked.last_error_category = ""
        else:
            locked.state = (
                ClanInvite.State.UNCERTAIN if result.ambiguous else ClanInvite.State.FAILED
            )
            locked.uncertain_since = now if result.ambiguous else None
            locked.last_error_category = result.category or "unknown"
        locked.save()
    if locked.state == ClanInvite.State.ACTIVE:
        dm = bot_api.send_message(
            target.telegram_user_id, f"Your Dexo Clan invite: {locked.invite_link}"
        )
        ClanInvite.objects.filter(pk=locked.pk).update(
            delivery_state=ClanInvite.Delivery.SENT if dm.ok else ClanInvite.Delivery.FAILED,
            delivery_error_category="" if dm.ok else (dm.category or "unknown"),
        )
    return ClanInvite.objects.get(pk=invite.pk)


def release_uncertain_invite(invite: ClanInvite) -> ClanInvite:
    """Operationally abandon an unknowable credential and release a JOIN reservation."""
    now = timezone.now()
    with transaction.atomic():
        invite = ClanInvite.objects.select_for_update().get(pk=invite.pk)
        if invite.state != ClanInvite.State.UNCERTAIN:
            return invite
        if (
            not invite.uncertain_since
            or invite.uncertain_since
            + timedelta(seconds=settings.CLAN_INVITE_UNCERTAIN_HOLD_SECONDS)
            > now
        ):
            raise ClanConflict("Uncertain credential safety hold is still active")
        invite.state = ClanInvite.State.REVOKED
        invite.last_error_category = "uncertain_released"
        invite.save()
        if invite.join_request_id:
            ClanJoinRequest.objects.filter(
                pk=invite.join_request_id, state=ClanJoinRequest.State.ACCEPTED
            ).update(state=ClanJoinRequest.State.CANCELLED)
        return invite


def _dispatch_revoke(invite_id: int) -> None:
    from apps.clans.tasks import revoke_clan_invite

    with suppress(Exception):
        revoke_clan_invite.delay(invite_id)


def queue_invite_revoke(invite_id: int, *, terminal: str) -> None:
    with transaction.atomic():
        invite = ClanInvite.objects.select_for_update().get(pk=invite_id)
        if not invite.invite_link or invite.state in {
            ClanInvite.State.REVOKED,
            ClanInvite.State.USED,
        }:
            invite.state = terminal
            invite.save(update_fields=("state", "updated_at"))
            return
        invite.state = ClanInvite.State.REVOKE_PENDING
        invite.desired_terminal_state = terminal
        invite.save(update_fields=("state", "desired_terminal_state", "updated_at"))
        transaction.on_commit(lambda: _dispatch_revoke(invite.pk))


def revoke_invite_remote(invite_id: int) -> ClanInvite:
    invite = ClanInvite.objects.select_related(
        "join_request__clan_season__clan__telegram_group",
        "membership__clan_season__clan__telegram_group",
    ).get(pk=invite_id)
    if invite.state not in {ClanInvite.State.REVOKE_PENDING, ClanInvite.State.REVOKE_FAILED}:
        return invite
    _, group = _invite_target(invite)
    result = bot_api.revoke_chat_invite_link(group.telegram_chat_id, invite.invite_link)
    with transaction.atomic():
        invite = ClanInvite.objects.select_for_update().get(pk=invite_id)
        if invite.state not in {ClanInvite.State.REVOKE_PENDING, ClanInvite.State.REVOKE_FAILED}:
            return invite
        invite.revoke_attempts += 1
        if result.ok:
            invite.state = invite.desired_terminal_state
            invite.last_error_category = ""
        else:
            invite.state = ClanInvite.State.REVOKE_FAILED
            invite.last_error_category = result.category or "unknown"
        invite.save()
        return invite


def get_or_create_rejoin_invite(player: Player, membership: ClanMembership) -> ClanInvite:
    if (
        membership.player_id != player.pk
        or membership.access_state != ClanMembership.Access.ACCESS_LOST
        or membership.ended_at is not None
    ):
        raise PermissionDenied("Recovery invite is unavailable")
    invite, _ = ClanInvite.objects.get_or_create(
        membership=membership,
        defaults={"purpose": ClanInvite.Purpose.REJOIN, "state": ClanInvite.State.PENDING},
    )
    return create_personal_invite(invite)


def finalize_accepted_request(req: ClanJoinRequest) -> ClanJoinRequest:
    """Freshly detect an already-present applicant without holding database locks."""
    req = ClanJoinRequest.objects.select_related(
        "applicant", "clan_season__clan__telegram_group", "invite"
    ).get(pk=req.pk)
    if req.state != ClanJoinRequest.State.ACCEPTED:
        return req
    result = bot_api.get_chat_member(
        req.clan_season.clan.telegram_group.telegram_chat_id, req.applicant.telegram_user_id
    )
    if result.ok and result.result:
        present, _, _ = membership_semantics(
            str(result.result.get("status", "")), result.result.get("is_member")
        )
        if present:
            reconcile_telegram_membership(
                player=req.applicant,
                group=req.clan_season.clan.telegram_group,
                present=True,
                telegram_update_id=0,
                observed_at=timezone.now(),
            )
            return ClanJoinRequest.objects.get(pk=req.pk)
    create_personal_invite(req.invite)
    return ClanJoinRequest.objects.get(pk=req.pk)


def reconcile_telegram_membership(
    *,
    player: Player,
    group: TelegramGroup,
    present: bool,
    telegram_update_id: int,
    observed_at: datetime,
) -> None:
    revoke_id: int | None = None
    with transaction.atomic():
        # Same applicant-first order used by accept/cancel/leave.
        Player.objects.select_for_update().get(pk=player.pk)
        membership = (
            ClanMembership.objects.select_for_update()
            .filter(player=player, clan_season__clan__telegram_group=group, ended_at__isnull=True)
            .first()
        )
        if membership:
            if telegram_update_id <= membership.last_telegram_update_id:
                return
            old_access = membership.access_state
            membership.last_telegram_update_id = telegram_update_id
            fields = ["last_telegram_update_id"]
            if present and old_access == ClanMembership.Access.ACCESS_LOST:
                membership.access_state = ClanMembership.Access.ACTIVE
                membership.access_lost_at = membership.access_rejoin_deadline = None
                fields += ["access_state", "access_lost_at", "access_rejoin_deadline"]
                ClanMembershipEvent.objects.create(
                    membership=membership,
                    kind="ACCESS_RESTORED",
                    telegram_update_id=telegram_update_id,
                    occurred_at=observed_at,
                )
                recovery = ClanInvite.objects.filter(membership=membership).first()
                if recovery and recovery.state == ClanInvite.State.ACTIVE:
                    revoke_id = recovery.pk
            elif not present and old_access == ClanMembership.Access.ACTIVE:
                policy = resolve_game_config_version(membership.config_version_id).clan
                assert policy is not None
                membership.access_state = ClanMembership.Access.ACCESS_LOST
                membership.access_lost_at = observed_at
                membership.access_rejoin_deadline = observed_at + timedelta(
                    hours=policy.access_rejoin_grace_hours
                )
                fields += ["access_state", "access_lost_at", "access_rejoin_deadline"]
                ClanMembershipEvent.objects.create(
                    membership=membership,
                    kind="ACCESS_LOST",
                    telegram_update_id=telegram_update_id,
                    occurred_at=observed_at,
                )
            membership.save(update_fields=(*fields,))
        elif present:
            req = (
                ClanJoinRequest.objects.filter(
                    applicant=player,
                    clan_season__clan__telegram_group=group,
                    state=ClanJoinRequest.State.ACCEPTED,
                )
                .select_related("clan_season")
                .first()
            )
            if req is None:
                return
            clan_season = ClanSeason.objects.select_for_update().get(pk=req.clan_season_id)
            req = (
                ClanJoinRequest.objects.select_for_update()
                .select_related("clan_season")
                .get(pk=req.pk)
            )
            # Source-time boundary permits delayed processing of pre-end events only.
            if not (clan_season.season.starts_at <= observed_at < clan_season.season.ends_at):
                return
            if clan_season.state not in {ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY}:
                return
            if ClanMembership.objects.filter(
                player=player, season=clan_season.season, ended_at__isnull=True
            ).exists():
                return
            membership = ClanMembership.objects.create(
                player=player,
                season=clan_season.season,
                clan_season=clan_season,
                role=ClanRole.SOLDIER,
                started_at=observed_at,
                last_telegram_update_id=telegram_update_id,
                config_version=clan_season.config_version,
            )
            req.state = ClanJoinRequest.State.ACTIVATED
            req.save(update_fields=("state",))
            invite = ClanInvite.objects.filter(join_request=req).first()
            if invite:
                revoke_id = invite.pk
            ClanMembershipEvent.objects.create(
                membership=membership,
                kind="ACTIVATED",
                telegram_update_id=telegram_update_id,
                occurred_at=observed_at,
            )
    if revoke_id is not None:
        queue_invite_revoke(revoke_id, terminal=ClanInvite.State.USED)


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
    invite_ids = list(
        ClanInvite.objects.filter(
            models.Q(join_request__applicant=player) | models.Q(membership=membership),
            state__in=[
                ClanInvite.State.CREATING,
                ClanInvite.State.ACTIVE,
                ClanInvite.State.FAILED,
                ClanInvite.State.UNCERTAIN,
            ],
        ).values_list("pk", flat=True)
    )
    for invite_id in invite_ids:
        transaction.on_commit(_revoke_after_leave(invite_id))
    return membership


def _revoke_after_leave(invite_id: int) -> Callable[[], None]:
    def callback() -> None:
        queue_invite_revoke(invite_id, terminal=ClanInvite.State.REVOKED)

    return callback
