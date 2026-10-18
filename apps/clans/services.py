# mypy: disable-error-code="misc"
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.clans.models import (
    Clan,
    ClanExpectedRemoval,
    ClanInvite,
    ClanJoinRequest,
    ClanMembership,
    ClanMembershipEvent,
    ClanMembershipIncident,
    ClanRecreationGrant,
    ClanRole,
    ClanSeason,
    ClanSeasonEvent,
    PlayerSeasonClanState,
)
from apps.clans.notifications import notify_clan_event
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ClanConfig, ResolvedGameConfig, resolve_game_config_version
from apps.players.models import Player
from apps.progression.services import refresh_player_progression
from apps.seasons.models import Season
from apps.telegram import bot_api
from apps.telegram.models import TelegramGroup, TelegramUserMembership
from apps.telegram.services import membership_semantics


class ClanConflict(ValidationError):
    pass


def _lock_invite_application(invite_id: int) -> None:
    """Within a transaction, follow applicant -> request -> invite ordering."""
    identity = ClanInvite.objects.values(
        "join_request_id", "join_request__applicant_id", "membership__player_id"
    ).get(pk=invite_id)
    applicant_id = identity["join_request__applicant_id"] or identity["membership__player_id"]
    Player.objects.select_for_update().get(pk=applicant_id)
    if identity["join_request_id"] is not None:
        ClanJoinRequest.objects.select_for_update().get(pk=identity["join_request_id"])


@transaction.atomic
def resolve_invite_credential_risk(
    *,
    invite_id: int,
    outcome: str,
    actor: User,
    reason: str,
    ticket_reference: str = "",
) -> ClanInvite:
    """Resolve an uncertain Telegram credential without bypassing its lifecycle."""
    if outcome not in {"CONFIRMED_REVOKED", "STILL_UNSAFE", "RETRY_REVOCATION"}:
        raise ValidationError("Unsupported invite-risk resolution outcome")
    if not reason.strip():
        raise ValidationError("Resolution reason is required")
    if not actor.has_perm("clans.resolve_invite_risk"):
        raise PermissionDenied("Invite-risk resolution permission required")
    _lock_invite_application(invite_id)
    invite = ClanInvite.objects.select_for_update().get(pk=invite_id)
    if invite.state != ClanInvite.State.MANUAL_REVIEW or not invite.unknown_credential_risk:
        raise ClanConflict("Invite is not awaiting credential-risk review")
    before = {"state": invite.state, "unknown_credential_risk": True}
    if outcome == "CONFIRMED_REVOKED":
        invite.state = ClanInvite.State.REVOKED
        invite.unknown_credential_risk = False
        invite.invite_link = ""
        invite.desired_terminal_state = ""
        invite.save(
            update_fields=(
                "state",
                "unknown_credential_risk",
                "invite_link",
                "desired_terminal_state",
                "updated_at",
            )
        )
        if invite.join_request_id:
            ClanJoinRequest.objects.filter(
                pk=invite.join_request_id, state=ClanJoinRequest.State.ACCEPTED
            ).update(state=ClanJoinRequest.State.CANCELLED, decided_at=timezone.now())
    elif outcome == "RETRY_REVOCATION":
        if not invite.invite_link:
            raise ClanConflict("A known credential is required for safe revocation retry")
        invite.state = ClanInvite.State.REVOKE_PENDING
        invite.next_revoke_retry_at = timezone.now()
        invite.save(update_fields=("state", "next_revoke_retry_at", "updated_at"))
    from apps.notifications.models import OperatorAuditEvent

    OperatorAuditEvent.objects.create(
        actor=actor,
        action=f"CLAN_INVITE_RISK_{outcome}",
        target_type="ClanInvite",
        target_id=str(invite.pk),
        reason=reason,
        ticket_reference=ticket_reference,
        before_state=before,
        after_state={
            "state": invite.state,
            "unknown_credential_risk": invite.unknown_credential_risk,
        },
    )
    return invite


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


def require_interactive_clan_season(clan_season: ClanSeason, *, at: datetime | None = None) -> None:
    now = at or timezone.now()
    if (
        clan_season.season.status != Season.Status.ACTIVE
        or not clan_season.season.starts_at <= now < clan_season.season.ends_at
        or clan_season.state != ClanSeason.State.ACTIVE
    ):
        raise ClanConflict("Clan Season is unavailable for new actions")


def unresolved_home_base_credential_exists(
    player: Player, clan_season: ClanSeason, *, exclude_invite_id: int | None = None
) -> bool:
    rows = ClanInvite.objects.filter(
        models.Q(
            join_request__applicant=player,
            join_request__clan_season__clan__telegram_group=clan_season.clan.telegram_group,
        )
        | models.Q(
            membership__player=player,
            membership__clan_season__clan__telegram_group=clan_season.clan.telegram_group,
        )
    ).filter(
        models.Q(unknown_credential_risk=True)
        | models.Q(
            state__in=[
                ClanInvite.State.CREATING,
                ClanInvite.State.REVOKE_PENDING,
                ClanInvite.State.REVOKE_FAILED,
            ]
        )
    )
    if exclude_invite_id is not None:
        rows = rows.exclude(pk=exclude_invite_id)
    return rows.exists()


def _other_active_join_request_exists(applicant_id: int, *, exclude_id: int | None = None) -> bool:
    """Caller holds applicant Player lock; active paths span Clans and Seasons."""
    rows = ClanJoinRequest.objects.filter(
        applicant_id=applicant_id,
        state__in=[ClanJoinRequest.State.PENDING, ClanJoinRequest.State.ACCEPTED],
    )
    if exclude_id is not None:
        rows = rows.exclude(pk=exclude_id)
    return rows.exists()


def unsafe_clan_credential_filter() -> models.Q:
    """Shared conservative predicate for write eligibility and owner-only reads."""
    return (
        models.Q(unknown_credential_risk=True)
        | models.Q(
            state__in=[
                ClanInvite.State.CREATING,
                ClanInvite.State.ACTIVE,
                ClanInvite.State.UNCERTAIN,
                ClanInvite.State.REVOKE_PENDING,
                ClanInvite.State.REVOKE_FAILED,
                ClanInvite.State.MANUAL_REVIEW,
            ]
        )
        | ~models.Q(invite_link="")
    )


def _unsafe_player_clan_credential_exists(
    applicant_id: int, *, exclude_invite_id: int | None = None
) -> bool:
    """Terminal request labels cannot hide unresolved JOIN or REJOIN credentials."""
    rows = ClanInvite.objects.filter(
        models.Q(join_request__applicant_id=applicant_id)
        | models.Q(membership__player_id=applicant_id)
    ).filter(unsafe_clan_credential_filter())
    if exclude_invite_id is not None:
        rows = rows.exclude(pk=exclude_invite_id)
    return rows.exists()


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
            bot_is_admin=True,
            bot_can_invite_users=True,
            title__gt="",
            clan__isnull=True,
        )
        .filter(
            models.Q(memberships__player=player, memberships__is_admin=True)
            | models.Q(candidates__player=player)
        )
        .exclude(
            clan__seasons__season=season,
            clan__seasons__state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
        )
        .exclude(
            models.Q(creator_registration__isnull=False)
            & ~models.Q(creator_registration__state="INACTIVE")
        )
        .distinct()
    )


def _require_no_network_context(group: TelegramGroup) -> None:
    from apps.network.models import CreatorGroupRegistration

    if CreatorGroupRegistration.objects.filter(group=group).exclude(state="INACTIVE").exists():
        raise ClanConflict("Telegram group already has a live economic context")


def _fresh_authority(player: Player, group: TelegramGroup) -> tuple[datetime, int]:
    _require_no_network_context(group)
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
    observed_id = (
        TelegramUserMembership.objects.filter(group=group, player=player)
        .values_list("last_update_id", flat=True)
        .first()
        or 0
    )
    _require_no_network_context(group)
    return timezone.now().replace(microsecond=0), observed_id


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
    from apps.clans.qa_recreation import recreation_grant_available

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
    if ClanSeason.objects.filter(
        captain=player, season=season, state=ClanSeason.State.ENDED
    ).exists() and not recreation_grant_available(player, season):
        raise ClanConflict("Same-Season Clan recreation is not allowed")
    group = eligible_groups(player, season, resolved).filter(pk=group_id).first()
    if group is None:
        raise PermissionDenied("Telegram group is not eligible")
    verified_at, observed_update_id = _fresh_authority(player, group)
    normalized = tag.strip().casefold()
    if not (2 <= len(name.strip()) <= 64 and 2 <= len(normalized) <= 12 and len(motto) <= 160):
        raise ClanConflict("Invalid Clan identity")
    config = GameConfigVersion.objects.get(pk=resolved.id)
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier
        from apps.seasons.services import resolve_current_season

        acquire_economic_write_barrier(season.pk)
        current_season = resolve_current_season(at=timezone.now())
        if current_season is None or current_season.pk != season.pk:
            raise ClanConflict("Clan Season is unavailable for new actions")
        group = TelegramGroup.objects.select_for_update().get(pk=group.pk)
        Player.objects.select_for_update().get(pk=player.pk)
        _require_no_network_context(group)
        existing = ClanSeason.objects.filter(
            captain=player,
            season=season,
            state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
        ).first()
        if existing:
            if existing.clan.telegram_group_id == group.pk:
                return existing
            raise ClanConflict("Captain already has a Clan this Season")
        recreation_grant = None
        if ClanSeason.objects.filter(
            captain=player, season=season, state=ClanSeason.State.ENDED
        ).exists():
            recreation_grant = (
                ClanRecreationGrant.objects.select_for_update()
                .filter(player=player, season=season, consumed_at__isnull=True)
                .first()
            )
            if recreation_grant is None:
                raise ClanConflict("Same-Season Clan recreation is not allowed")
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
        if recreation_grant is not None:
            from apps.clans.qa_recreation import consume_qa_recreation

            consume_qa_recreation(recreation_grant, clan_season)
        if not group.is_monitored:
            group.is_monitored = True
            group.save(update_fields=("is_monitored", "updated_at"))
        membership = ClanMembership.objects.create(
            player=player,
            season=season,
            clan_season=clan_season,
            role=ClanRole.CAPTAIN,
            started_at=timezone.now(),
            last_clan_active_at=timezone.now(),
            next_inactivity_check_at=timezone.now() + timedelta(days=policy.captain_warning_days),
            last_telegram_update_id=observed_update_id,
            telegram_verified_at=verified_at,
            config_version=config,
        )
        activated_event = ClanMembershipEvent.objects.create(
            membership=membership, kind="ACTIVATED", occurred_at=membership.started_at
        )
        notify_clan_event(
            recipient=membership.player,
            event_key="CLAN_MEMBERSHIP_ACTIVATED",
            source_type="CLAN_MEMBERSHIP_EVENT",
            source_id=activated_event.pk,
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
    if unresolved_home_base_credential_exists(player, clan_season):
        raise ClanConflict("A previous unknown Telegram credential requires manual review")
    if clan_season.state != ClanSeason.State.ACTIVE or not clan_season.accepting_members:
        raise ClanConflict("Clan is not accepting members")
    request = ClanJoinRequest.objects.filter(
        clan_season=clan_season,
        applicant=player,
        state__in=[ClanJoinRequest.State.PENDING, ClanJoinRequest.State.ACCEPTED],
    ).first()
    if request is not None:
        return request
    if _other_active_join_request_exists(player.pk) or _unsafe_player_clan_credential_exists(
        player.pk
    ):
        raise ClanConflict("An existing Clan application must be safely resolved first")
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
            require_interactive_clan_season(clan_season)
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
            if _other_active_join_request_exists(
                identity["applicant_id"], exclude_id=req.pk
            ) or _unsafe_player_clan_credential_exists(identity["applicant_id"]):
                raise ClanConflict("An existing Clan application must be safely resolved first")
            occupied = clan_season.memberships.filter(ended_at__isnull=True).count()
            reserved = clan_season.join_requests.filter(
                state=ClanJoinRequest.State.ACCEPTED
            ).count()
            from apps.clans.economy import effective_capacity

            if occupied + reserved >= effective_capacity(clan_season, policy):
                raise ClanConflict("Clan capacity is full")
            req.state = ClanJoinRequest.State.ACCEPTED
            ClanInvite.objects.create(join_request=req, purpose=ClanInvite.Purpose.JOIN)
        req.decided_at = timezone.now()
        req.save(update_fields=("state", "decided_at"))
        notify_clan_event(
            recipient=req.applicant,
            event_key="CLAN_JOIN_ACCEPTED" if accept else "CLAN_JOIN_DECLINED",
            source_type="CLAN_JOIN_REQUEST",
            source_id=req.pk,
        )
        return req


def cancel_request(player: Player, request_id: int) -> ClanJoinRequest:
    with transaction.atomic():
        Player.objects.select_for_update().get(pk=player.pk)
        req = ClanJoinRequest.objects.select_for_update().get(pk=request_id, applicant=player)
        if req.state == ClanJoinRequest.State.PENDING:
            req.state = ClanJoinRequest.State.CANCELLED
            req.save(update_fields=("state",))
        elif req.state == ClanJoinRequest.State.ACCEPTED:
            invite = ClanInvite.objects.select_for_update().get(join_request=req)
            queue_invite_revoke(invite.pk, terminal=ClanInvite.State.REVOKED)
        return req


def captain_cancel_accepted_request(player: Player, request_id: int) -> ClanJoinRequest:
    applicant_id = ClanJoinRequest.objects.values_list("applicant_id", flat=True).get(pk=request_id)
    with transaction.atomic():
        Player.objects.select_for_update().get(pk=applicant_id)
        req = (
            ClanJoinRequest.objects.select_for_update()
            .select_related("clan_season")
            .get(pk=request_id)
        )
        if req.clan_season.captain_id != player.pk:
            raise PermissionDenied("Captain role required")
        if req.state != ClanJoinRequest.State.ACCEPTED:
            raise ClanConflict("Only an accepted path can be revoked")
        invite = ClanInvite.objects.select_for_update().get(join_request=req)
        queue_invite_revoke(invite.pk, terminal=ClanInvite.State.REVOKED)
        return req


def _invite_target(invite: ClanInvite) -> tuple[Player, TelegramGroup]:
    if invite.purpose == ClanInvite.Purpose.JOIN:
        assert invite.join_request is not None
        return invite.join_request.applicant, invite.join_request.clan_season.clan.telegram_group
    assert invite.membership is not None
    return invite.membership.player, invite.membership.clan_season.clan.telegram_group


def create_personal_invite(invite: ClanInvite) -> ClanInvite:
    """Claim in PostgreSQL, call Telegram after commit, then reconcile by token."""
    invite = ClanInvite.objects.select_related(
        "join_request__clan_season__season", "membership__clan_season__season"
    ).get(pk=invite.pk)
    if invite.join_request_id:
        assert invite.join_request is not None
        clan_season = invite.join_request.clan_season
    else:
        assert invite.membership is not None
        clan_season = invite.membership.clan_season
    require_interactive_clan_season(clan_season)
    now = timezone.now()
    dispatch_revoke = False
    with transaction.atomic():
        target, _ = _invite_target(invite)
        _lock_invite_application(invite.pk)
        locked = ClanInvite.objects.select_for_update().get(pk=invite.pk)
        if locked.state in {
            ClanInvite.State.ACTIVE,
            ClanInvite.State.USED,
            ClanInvite.State.REVOKED,
            ClanInvite.State.REVOKE_PENDING,
            ClanInvite.State.REVOKE_FAILED,
            ClanInvite.State.MANUAL_REVIEW,
        }:
            return locked
        if locked.state == ClanInvite.State.UNCERTAIN:
            return locked
        if (
            locked.state == ClanInvite.State.FAILED
            and locked.next_create_retry_at is not None
            and locked.next_create_retry_at > now
        ):
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
            locked.unknown_credential_risk = True
            locked.save()
            return locked
        if locked.attempt_count >= settings.CLAN_INVITE_MAX_CREATE_ATTEMPTS:
            locked.state = ClanInvite.State.UNCERTAIN
            locked.uncertain_since = now
            locked.last_error_category = "attempt_limit"
            locked.save()
            return locked
        if locked.purpose == ClanInvite.Purpose.JOIN:
            if not ClanJoinRequest.objects.filter(
                pk=locked.join_request_id, state=ClanJoinRequest.State.ACCEPTED
            ).exists():
                raise ClanConflict("Join request is no longer accepted")
            if _other_active_join_request_exists(
                target.pk, exclude_id=locked.join_request_id
            ) or _unsafe_player_clan_credential_exists(target.pk, exclude_invite_id=locked.pk):
                raise ClanConflict("An existing Clan application must be safely resolved first")
        elif not ClanMembership.objects.filter(
            pk=locked.membership_id,
            ended_at__isnull=True,
            access_state=ClanMembership.Access.ACCESS_LOST,
        ).exists():
            raise ClanConflict("Recovery membership is no longer eligible")
        token = uuid.uuid4()
        locked.state = ClanInvite.State.CREATING
        locked.claim_token = token
        locked.claim_expires_at = now + timedelta(seconds=settings.CLAN_INVITE_CREATE_LEASE_SECONDS)
        locked.next_create_retry_at = None
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
        _lock_invite_application(invite.pk)
        locked = ClanInvite.objects.select_for_update().get(pk=invite.pk)
        if locked.state != ClanInvite.State.CREATING or locked.claim_token != token:
            return locked
        locked.claim_token = None
        locked.claim_expires_at = None
        if result.ok and result.result and isinstance(result.result.get("invite_link"), str):
            locked.invite_link = result.result["invite_link"]
            locked.last_error_category = ""
            locked.next_create_retry_at = None
            locked.unknown_credential_risk = False
            if locked.desired_terminal_state:
                locked.state = ClanInvite.State.REVOKE_PENDING
                locked.next_revoke_retry_at = timezone.now()
                dispatch_revoke = True
            else:
                locked.state = ClanInvite.State.ACTIVE
        else:
            locked.state = (
                ClanInvite.State.UNCERTAIN if result.ambiguous else ClanInvite.State.FAILED
            )
            locked.uncertain_since = now if result.ambiguous else None
            locked.unknown_credential_risk = result.ambiguous
            locked.last_error_category = result.category or "unknown"
            if result.ambiguous:
                pass
            elif locked.desired_terminal_state:
                locked.state = locked.desired_terminal_state
                locked.desired_terminal_state = ""
            else:
                delay = (
                    result.retry_after
                    if result.category == "rate_limited" and result.retry_after is not None
                    else min(
                        settings.CLAN_INVITE_RETRY_MAX_SECONDS,
                        settings.CLAN_INVITE_RETRY_BASE_SECONDS
                        * (2 ** max(locked.attempt_count - 1, 0)),
                    )
                )
                locked.next_create_retry_at = now + timedelta(seconds=delay)
        locked.save()
        if (
            locked.join_request_id
            and locked.state == ClanInvite.State.REVOKED
            and not locked.unknown_credential_risk
            and not locked.invite_link
        ):
            ClanJoinRequest.objects.filter(
                pk=locked.join_request_id, state=ClanJoinRequest.State.ACCEPTED
            ).update(state=ClanJoinRequest.State.CANCELLED, decided_at=timezone.now())
    if dispatch_revoke:
        _dispatch_revoke(locked.pk)
    elif locked.state == ClanInvite.State.ACTIVE:
        from apps.notifications.services import emit_notification

        # Credential retrieval belongs exclusively to the authenticated Mini App.
        emit_notification(
            recipient=target,
            event_key="CLAN_INVITE_READY",
            source_domain="CLAN",
            source_type="CLAN_INVITE",
            source_id=str(locked.pk),
            idempotency_key=f"clan-invite-ready:{locked.pk}",
            payload={"invite_id": locked.pk},
            telegram=False,
        )
    return ClanInvite.objects.get(pk=invite.pk)


def release_uncertain_invite(invite: ClanInvite) -> ClanInvite:
    """Unknown remote credentials cannot be released without positive revocation proof."""
    invite = ClanInvite.objects.get(pk=invite.pk)
    if invite.state == ClanInvite.State.UNCERTAIN:
        raise ClanConflict("Uncertain credential requires safe revocation or manual review")
    return invite


def recover_invite_creations(*, limit: int | None = None) -> int:
    """Advance stale/ambiguous invite rows without holding locks across Telegram HTTP."""
    now = timezone.now()
    hold_before = now - timedelta(seconds=settings.CLAN_INVITE_UNCERTAIN_HOLD_SECONDS)
    lease_before = now - timedelta(seconds=settings.CLAN_INVITE_CREATE_LEASE_SECONDS)
    batch = limit or settings.TELEGRAM_INGRESS_REDRIVE_BATCH_SIZE
    retry_ids: list[tuple[int, str]] = []
    inactive_ids: list[int] = []
    with transaction.atomic():
        rows = list(
            ClanInvite.objects.select_for_update(skip_locked=True)
            .filter(
                models.Q(state=ClanInvite.State.CREATING)
                & (
                    models.Q(claim_expires_at__lte=now)
                    | models.Q(claim_expires_at__isnull=True, updated_at__lte=lease_before)
                )
                | models.Q(state=ClanInvite.State.UNCERTAIN, uncertain_since__lte=hold_before)
                | models.Q(state=ClanInvite.State.FAILED, next_create_retry_at__lte=now)
                | models.Q(state=ClanInvite.State.PENDING)
            )
            .order_by("updated_at", "pk")[:batch]
        )
        for invite in rows:
            if invite.purpose == ClanInvite.Purpose.JOIN:
                target_valid = bool(
                    invite.join_request_id
                    and ClanJoinRequest.objects.filter(
                        pk=invite.join_request_id,
                        state=ClanJoinRequest.State.ACCEPTED,
                        clan_season__season__status=Season.Status.ACTIVE,
                        clan_season__season__starts_at__lte=now,
                        clan_season__season__ends_at__gt=now,
                        clan_season__state=ClanSeason.State.ACTIVE,
                    ).exists()
                )
            else:
                target_valid = bool(
                    invite.membership_id
                    and ClanMembership.objects.filter(
                        pk=invite.membership_id,
                        ended_at__isnull=True,
                        access_state=ClanMembership.Access.ACCESS_LOST,
                        clan_season__season__status=Season.Status.ACTIVE,
                        clan_season__season__starts_at__lte=now,
                        clan_season__season__ends_at__gt=now,
                        clan_season__state=ClanSeason.State.ACTIVE,
                    ).exists()
                )
            if not target_valid:
                if invite.state in {ClanInvite.State.CREATING, ClanInvite.State.UNCERTAIN}:
                    # Inactive context is not proof that a remote credential vanished.
                    invite.state = ClanInvite.State.MANUAL_REVIEW
                    invite.unknown_credential_risk = True
                    invite.desired_terminal_state = ClanInvite.State.REVOKED
                    invite.claim_token = None
                    invite.claim_expires_at = None
                    invite.last_error_category = "inactive_target_credential_uncertain"
                    invite.save()
                else:
                    inactive_ids.append(invite.pk)
                continue
            if invite.state == ClanInvite.State.CREATING:
                invite.state = ClanInvite.State.UNCERTAIN
                invite.uncertain_since = now
                invite.claim_token = None
                invite.claim_expires_at = None
                invite.last_error_category = "create_lease_expired"
                invite.unknown_credential_risk = True
                invite.save()
                continue
            if invite.state == ClanInvite.State.UNCERTAIN and invite.desired_terminal_state:
                invite.state = ClanInvite.State.MANUAL_REVIEW
                invite.unknown_credential_risk = True
                invite.last_error_category = "uncertain_terminalization"
                invite.save()
                continue
            if invite.attempt_count >= settings.CLAN_INVITE_MAX_CREATE_ATTEMPTS:
                invite.state = ClanInvite.State.MANUAL_REVIEW
                invite.next_create_retry_at = None
                invite.last_error_category = "create_attempt_limit"
                invite.save()
                continue
            if (
                invite.state == ClanInvite.State.UNCERTAIN
                and invite.purpose == ClanInvite.Purpose.JOIN
            ):
                # Unknown JOIN credentials are not duplicated and retain their reservation.
                invite.state = ClanInvite.State.MANUAL_REVIEW
                invite.unknown_credential_risk = True
                invite.last_error_category = "uncertain_join_manual_review"
                invite.save()
                continue
            invite.state = ClanInvite.State.PENDING
            invite.next_create_retry_at = None
            invite.save(update_fields=("state", "next_create_retry_at", "updated_at"))
            retry_ids.append((invite.pk, invite.purpose))
    for invite_id in inactive_ids:
        queue_invite_revoke(invite_id, terminal=ClanInvite.State.REVOKED)
    for invite_id, purpose in retry_ids:
        invite = ClanInvite.objects.select_related("join_request").get(pk=invite_id)
        try:
            if purpose == ClanInvite.Purpose.JOIN:
                assert invite.join_request is not None
                finalize_accepted_request(invite.join_request)
            else:
                create_personal_invite(invite)
        except ClanConflict:
            # Legacy overlapping applications need explicit resolution. One
            # blocked row must not prevent safe redrive of the rest of the batch.
            continue
    return len(rows)


def _dispatch_revoke(invite_id: int) -> None:
    from apps.clans.tasks import revoke_clan_invite

    with suppress(Exception):
        revoke_clan_invite.delay(invite_id)


def queue_invite_revoke(invite_id: int, *, terminal: str) -> None:
    with transaction.atomic():
        _lock_invite_application(invite_id)
        invite = ClanInvite.objects.select_for_update().get(pk=invite_id)
        if invite.state == ClanInvite.State.CREATING:
            invite.desired_terminal_state = terminal
            invite.save(update_fields=("desired_terminal_state", "updated_at"))
            return
        if invite.state == ClanInvite.State.UNCERTAIN:
            invite.desired_terminal_state = terminal
            invite.unknown_credential_risk = True
            invite.save(
                update_fields=("desired_terminal_state", "unknown_credential_risk", "updated_at")
            )
            return
        if invite.state in {
            ClanInvite.State.REVOKED,
            ClanInvite.State.USED,
        }:
            if invite.unknown_credential_risk or invite.invite_link:
                # Legacy terminal labels are not revocation proof when risk or
                # a credential survives. Keep them in the existing review flow.
                invite.state = ClanInvite.State.MANUAL_REVIEW
                invite.unknown_credential_risk = True
                invite.desired_terminal_state = terminal
                invite.save(
                    update_fields=(
                        "state",
                        "unknown_credential_risk",
                        "desired_terminal_state",
                        "updated_at",
                    )
                )
            elif invite.join_request_id and terminal == ClanInvite.State.REVOKED:
                ClanJoinRequest.objects.filter(
                    pk=invite.join_request_id, state=ClanJoinRequest.State.ACCEPTED
                ).update(state=ClanJoinRequest.State.CANCELLED, decided_at=timezone.now())
            return
        if invite.unknown_credential_risk and not invite.invite_link:
            invite.desired_terminal_state = terminal
            invite.state = ClanInvite.State.MANUAL_REVIEW
            invite.save(update_fields=("desired_terminal_state", "state", "updated_at"))
            return
        if not invite.invite_link and invite.state in {
            ClanInvite.State.ACTIVE,
            ClanInvite.State.REVOKE_PENDING,
            ClanInvite.State.REVOKE_FAILED,
            ClanInvite.State.MANUAL_REVIEW,
        }:
            # Missing legacy metadata cannot prove nonissuance or revocation.
            invite.state = ClanInvite.State.MANUAL_REVIEW
            invite.unknown_credential_risk = True
            invite.desired_terminal_state = terminal
            invite.save(
                update_fields=(
                    "state",
                    "unknown_credential_risk",
                    "desired_terminal_state",
                    "updated_at",
                )
            )
            return
        if not invite.invite_link:
            invite.state = terminal
            invite.desired_terminal_state = ""
            invite.save(update_fields=("state", "desired_terminal_state", "updated_at"))
            if invite.join_request_id and terminal == ClanInvite.State.REVOKED:
                ClanJoinRequest.objects.filter(
                    pk=invite.join_request_id, state=ClanJoinRequest.State.ACCEPTED
                ).update(state=ClanJoinRequest.State.CANCELLED, decided_at=timezone.now())
            return
        invite.state = ClanInvite.State.REVOKE_PENDING
        invite.desired_terminal_state = terminal
        invite.next_revoke_retry_at = timezone.now()
        invite.save(
            update_fields=("state", "desired_terminal_state", "next_revoke_retry_at", "updated_at")
        )
        transaction.on_commit(lambda: _dispatch_revoke(invite.pk))


def revoke_invite_remote(invite_id: int) -> ClanInvite:
    now = timezone.now()
    with transaction.atomic():
        _lock_invite_application(invite_id)
        invite = ClanInvite.objects.select_for_update().get(pk=invite_id)
        if invite.state not in {ClanInvite.State.REVOKE_PENDING, ClanInvite.State.REVOKE_FAILED}:
            return invite
        if invite.next_revoke_retry_at and invite.next_revoke_retry_at > now:
            return invite
        if invite.revoke_claim_expires_at is not None and invite.revoke_claim_expires_at > now:
            return invite
        if invite.revoke_attempts >= settings.CLAN_INVITE_MAX_REVOKE_ATTEMPTS:
            invite.state = ClanInvite.State.MANUAL_REVIEW
            invite.unknown_credential_risk = True
            invite.next_revoke_retry_at = None
            invite.last_error_category = "revoke_attempt_limit"
            invite.save()
            return invite
        token = uuid.uuid4()
        invite.revoke_claim_token = token
        invite.revoke_claim_expires_at = now + timedelta(
            seconds=settings.CLAN_INVITE_REVOKE_LEASE_SECONDS
        )
        invite.revoke_attempts += 1
        invite.save()
    invite = ClanInvite.objects.select_related(
        "join_request__clan_season__clan__telegram_group",
        "membership__clan_season__clan__telegram_group",
    ).get(pk=invite_id)
    _, group = _invite_target(invite)
    result = bot_api.revoke_chat_invite_link(group.telegram_chat_id, invite.invite_link)
    with transaction.atomic():
        _lock_invite_application(invite_id)
        invite = ClanInvite.objects.select_for_update().get(pk=invite_id)
        if invite.revoke_claim_token != token:
            return invite
        invite.revoke_claim_token = None
        invite.revoke_claim_expires_at = None
        if result.ok:
            invite.state = invite.desired_terminal_state
            invite.last_error_category = ""
            invite.next_revoke_retry_at = None
            invite.invite_link = ""
            invite.unknown_credential_risk = False
            if invite.join_request_id and invite.state == ClanInvite.State.REVOKED:
                ClanJoinRequest.objects.filter(
                    pk=invite.join_request_id, state=ClanJoinRequest.State.ACCEPTED
                ).update(state=ClanJoinRequest.State.CANCELLED, decided_at=now)
        else:
            invite.state = ClanInvite.State.REVOKE_FAILED
            invite.unknown_credential_risk = True
            invite.last_error_category = result.category or "unknown"
            delay = (
                result.retry_after
                if result.retry_after is not None
                else min(
                    settings.CLAN_INVITE_REVOKE_RETRY_MAX_SECONDS,
                    settings.CLAN_INVITE_REVOKE_RETRY_BASE_SECONDS
                    * (2 ** max(invite.revoke_attempts - 1, 0)),
                )
            )
            invite.next_revoke_retry_at = now + timedelta(seconds=delay)
        invite.save()
        return invite


def get_or_create_rejoin_invite(player: Player, membership: ClanMembership) -> ClanInvite:
    if (
        membership.player_id != player.pk
        or membership.access_state != ClanMembership.Access.ACCESS_LOST
        or membership.ended_at is not None
    ):
        raise PermissionDenied("Recovery invite is unavailable")
    require_interactive_clan_season(membership.clan_season)
    invite, created = ClanInvite.objects.get_or_create(
        membership=membership,
        defaults={"purpose": ClanInvite.Purpose.REJOIN, "state": ClanInvite.State.PENDING},
    )
    if not created and invite.state in {ClanInvite.State.USED, ClanInvite.State.REVOKED}:
        if invite.invite_link or invite.unknown_credential_risk:
            raise ClanConflict("Previous recovery credential is not safely terminal")
        invite.state = ClanInvite.State.PENDING
        invite.attempt_count = 0
        invite.claim_token = None
        invite.claim_expires_at = None
        invite.uncertain_since = None
        invite.next_create_retry_at = None
        invite.revoke_attempts = 0
        invite.revoke_claim_token = None
        invite.revoke_claim_expires_at = None
        invite.next_revoke_retry_at = None
        invite.desired_terminal_state = ""
        invite.last_error_category = ""
        invite.recovery_cycle += 1
        invite.save()
    if unresolved_home_base_credential_exists(
        player, membership.clan_season, exclude_invite_id=invite.pk
    ):
        raise ClanConflict("Another Home Base credential requires manual review")
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
            verified_at = timezone.now().replace(microsecond=0)
            reconcile_telegram_membership(
                player=req.applicant,
                group=req.clan_season.clan.telegram_group,
                present=True,
                telegram_update_id=0,
                observed_at=verified_at,
                fresh_verified_at=verified_at,
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
    fresh_verified_at: datetime | None = None,
    telegram_actor_user_id: int | None = None,
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
        if membership is None and not present:
            expected = (
                ClanExpectedRemoval.objects.select_for_update()
                .select_related("membership")
                .filter(
                    membership__player=player,
                    membership__clan_season__clan__telegram_group=group,
                    reconciled_at__isnull=True,
                    created_at__lte=observed_at,
                    membership__last_telegram_update_id__lt=telegram_update_id,
                )
                .order_by("-created_at", "-pk")
                .first()
            )
            if expected is not None:
                expected.reconciled_at = observed_at
                expected.save(update_fields=("reconciled_at",))
                expected.membership.last_telegram_update_id = telegram_update_id
                expected.membership.save(update_fields=("last_telegram_update_id",))
                return
        if membership:
            if telegram_update_id <= membership.last_telegram_update_id:
                return
            if (
                membership.telegram_verified_at is not None
                and observed_at < membership.telegram_verified_at
            ):
                return
            old_access = membership.access_state
            membership.last_telegram_update_id = telegram_update_id
            fields = ["last_telegram_update_id"]
            if present and old_access == ClanMembership.Access.ACCESS_LOST:
                membership.access_state = ClanMembership.Access.ACTIVE
                membership.access_lost_at = membership.access_rejoin_deadline = None
                fields += ["access_state", "access_lost_at", "access_rejoin_deadline"]
                restored_event = ClanMembershipEvent.objects.create(
                    membership=membership,
                    kind="ACCESS_RESTORED",
                    telegram_update_id=telegram_update_id,
                    occurred_at=observed_at,
                )
                notify_clan_event(
                    recipient=membership.player,
                    event_key="CLAN_ACCESS_RESTORED",
                    source_type="CLAN_MEMBERSHIP_EVENT",
                    source_id=restored_event.pk,
                )
                recovery = ClanInvite.objects.filter(membership=membership).first()
                if recovery and recovery.state not in {
                    ClanInvite.State.USED,
                    ClanInvite.State.REVOKED,
                }:
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
                ClanMembershipIncident.objects.get_or_create(
                    membership=membership,
                    telegram_update_id=telegram_update_id,
                    defaults={
                        "telegram_actor_user_id": telegram_actor_user_id,
                        "observed_at": observed_at,
                    },
                )
            membership.save(update_fields=(*fields,))
            if not present and old_access == ClanMembership.Access.ACTIVE:
                from apps.notifications.services import emit_notification_safely

                transaction.on_commit(
                    lambda member=membership: emit_notification_safely(
                        recipient=member.player,
                        event_key="CLAN_ACCESS_LOST",
                        source_domain="CLAN",
                        source_type="CLAN_MEMBERSHIP",
                        source_id=str(member.pk),
                        idempotency_key=f"clan-access-lost:{member.pk}:{telegram_update_id}",
                        payload={"membership_id": member.pk},
                        telegram=True,
                    )
                )
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
            # Telegram source timestamps have one-second precision: only the
            # exact acceptance source-second is boundary-equivalent.
            if req.decided_at is None or observed_at.replace(
                microsecond=0
            ) < req.decided_at.replace(microsecond=0):
                return
            if ClanMembership.objects.filter(
                player=player, season=clan_season.season, ended_at__isnull=True
            ).exists():
                return
            policy = resolve_game_config_version(clan_season.config_version_id).clan
            assert policy is not None
            membership = ClanMembership.objects.create(
                player=player,
                season=clan_season.season,
                clan_season=clan_season,
                role=ClanRole.SOLDIER,
                started_at=max(observed_at, req.decided_at),
                last_clan_active_at=max(observed_at, req.decided_at),
                next_inactivity_check_at=max(observed_at, req.decided_at)
                + timedelta(days=policy.soldier_warning_days),
                last_telegram_update_id=telegram_update_id,
                telegram_verified_at=fresh_verified_at,
                config_version=clan_season.config_version,
            )
            req.state = ClanJoinRequest.State.ACTIVATED
            req.save(update_fields=("state",))
            invite = ClanInvite.objects.filter(join_request=req).first()
            if invite:
                revoke_id = invite.pk
            activated_event = ClanMembershipEvent.objects.create(
                membership=membership,
                kind="ACTIVATED",
                telegram_update_id=telegram_update_id,
                occurred_at=observed_at,
            )
            notify_clan_event(
                recipient=membership.player,
                event_key="CLAN_MEMBERSHIP_ACTIVATED",
                source_type="CLAN_MEMBERSHIP_EVENT",
                source_id=activated_event.pk,
            )
    if revoke_id is not None:
        queue_invite_revoke(revoke_id, terminal=ClanInvite.State.USED)


@transaction.atomic
def voluntary_leave(
    player: Player, season: Season, resolved: ResolvedGameConfig
) -> ClanMembership | None:
    policy = require_policy(resolved)
    Player.objects.select_for_update().get(pk=player.pk)
    membership = (
        ClanMembership.objects.select_for_update()
        .filter(player=player, season=season, ended_at__isnull=True)
        .first()
    )
    if membership is None:
        return None
    state = PlayerSeasonClanState.objects.select_for_update().get(player=player, season=season)
    if membership.role != ClanRole.SOLDIER:
        raise PermissionDenied("Captain cannot use voluntary leave")
    if state.voluntary_leave_count >= policy.max_voluntary_leaves_per_season:
        raise ClanConflict("Season voluntary leave limit reached")
    now = timezone.now()
    ClanExpectedRemoval.objects.get_or_create(
        membership=membership,
        defaults={
            "reason": ClanMembership.EndReason.VOLUNTARY_LEAVE,
            "source_type": "VOLUNTARY_LEAVE",
            "source_id": str(membership.pk),
        },
    )
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


def close_season_clan_work(season_id: int, *, limit: int = 200) -> dict[str, int]:
    """Advance S1 credentials through existing cancellation/revocation state machines."""
    request_ids = list(
        ClanJoinRequest.objects.filter(
            clan_season__season_id=season_id,
            state__in=(ClanJoinRequest.State.PENDING, ClanJoinRequest.State.ACCEPTED),
        )
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )
    for request_id in request_ids:
        request = ClanJoinRequest.objects.select_related("applicant", "clan_season").get(
            pk=request_id
        )
        cancel_request(request.applicant, request_id)
        ClanSeasonEvent.objects.get_or_create(
            clan_season=request.clan_season,
            kind="SEASON_CLOSE_REQUEST_DISPOSITION",
            source_type="JOIN_REQUEST",
            source_id=str(request.pk),
            defaults={"occurred_at": timezone.now(), "metadata": {"prior_state": request.state}},
        )
    invite_ids = list(
        ClanInvite.objects.filter(
            models.Q(join_request__clan_season__season_id=season_id)
            | models.Q(membership__season_id=season_id),
            state__in=(
                ClanInvite.State.PENDING,
                ClanInvite.State.CREATING,
                ClanInvite.State.ACTIVE,
                ClanInvite.State.FAILED,
                ClanInvite.State.REVOKE_FAILED,
            ),
        )
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )
    for invite_id in invite_ids:
        invite = ClanInvite.objects.select_related(
            "join_request__clan_season", "membership__clan_season"
        ).get(pk=invite_id)
        queue_invite_revoke(invite_id, terminal=ClanInvite.State.REVOKED)
        if invite.join_request_id:
            assert invite.join_request is not None
            clan_season = invite.join_request.clan_season
        else:
            assert invite.membership is not None
            clan_season = invite.membership.clan_season
        ClanSeasonEvent.objects.get_or_create(
            clan_season=clan_season,
            kind="SEASON_CLOSE_INVITE_DISPOSITION",
            source_type="CLAN_INVITE",
            source_id=str(invite.pk),
            defaults={"occurred_at": timezone.now(), "metadata": {"prior_state": invite.state}},
        )
    unresolved = ClanInvite.objects.filter(
        models.Q(join_request__clan_season__season_id=season_id)
        | models.Q(membership__season_id=season_id),
        state__in=(
            ClanInvite.State.PENDING,
            ClanInvite.State.CREATING,
            ClanInvite.State.ACTIVE,
            ClanInvite.State.FAILED,
            ClanInvite.State.UNCERTAIN,
            ClanInvite.State.REVOKE_PENDING,
            ClanInvite.State.REVOKE_FAILED,
            ClanInvite.State.MANUAL_REVIEW,
        ),
    ).count()
    ended = 0
    if (
        not unresolved
        and not ClanJoinRequest.objects.filter(
            clan_season__season_id=season_id,
            state__in=(ClanJoinRequest.State.PENDING, ClanJoinRequest.State.ACCEPTED),
        ).exists()
    ):
        for clan_season in ClanSeason.objects.filter(season_id=season_id).exclude(
            state=ClanSeason.State.ENDED
        ):
            with transaction.atomic():
                locked = ClanSeason.objects.select_for_update().get(pk=clan_season.pk)
                locked.state = ClanSeason.State.ENDED
                locked.accepting_members = False
                locked.save(update_fields=("state", "accepting_members", "updated_at"))
                ClanSeasonEvent.objects.get_or_create(
                    clan_season=locked,
                    kind="SEASON_CLOSE_ENDED",
                    source_type="SEASON_CLOSE",
                    source_id=str(season_id),
                    defaults={"occurred_at": timezone.now()},
                )
                ended += 1
    return {
        "requests_visited": len(request_ids),
        "invites_visited": len(invite_ids),
        "unresolved": unresolved,
        "ended": ended,
    }


def terminalize_closed_clan_credentials(*, limit: int = 100) -> int:
    """Bounded scanner preserves unresolved reservations until safe remote revocation."""
    requests = list(
        ClanJoinRequest.objects.filter(
            clan_season__state=ClanSeason.State.ENDED,
            state__in=[ClanJoinRequest.State.PENDING, ClanJoinRequest.State.ACCEPTED],
        )
        .select_related("applicant")
        .order_by("pk")[:limit]
    )
    for row in requests:
        cancel_request(row.applicant, row.pk)
    ids = list(
        ClanInvite.objects.filter(
            models.Q(join_request__clan_season__state=ClanSeason.State.ENDED)
            | models.Q(membership__ended_at__isnull=False),
            state__in=[
                ClanInvite.State.PENDING,
                ClanInvite.State.ACTIVE,
                ClanInvite.State.FAILED,
                ClanInvite.State.CREATING,
                ClanInvite.State.UNCERTAIN,
            ],
        )
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )
    for invite_id in ids:
        queue_invite_revoke(invite_id, terminal=ClanInvite.State.REVOKED)
    return len(requests) + len(ids)
