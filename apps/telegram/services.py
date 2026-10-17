import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

import structlog
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import F
from django.utils import timezone

from apps.players.models import Player
from apps.players.services import PlayerResolution, TelegramProfile, resolve_player
from apps.telegram.events import (
    BotMembershipChanged,
    BotStarted,
    TelegramMessageObserved,
    UserMembershipChanged,
)
from apps.telegram.models import (
    TelegramDeadLetterDisposition,
    TelegramGroup,
    TelegramGroupCandidate,
    TelegramInboundUpdate,
    TelegramUserMembership,
)
from apps.telegram.normalizer import normalize_update

logger = structlog.get_logger(__name__)

TERMINAL_INGRESS_STATES = {
    TelegramInboundUpdate.State.PROCESSED,
    TelegramInboundUpdate.State.IGNORED,
    TelegramInboundUpdate.State.DEAD_LETTER,
}


def resolve_dead_letter(
    inbound_id: int,
    *,
    outcome: str,
    actor: Any | None,
    service_actor: str = "",
    reason: str,
    evidence_reference: str,
    idempotency_key: str,
) -> TelegramDeadLetterDisposition:
    """Disposition one poison ingress row without changing its identity or failure history."""
    if actor is not None and not actor.has_perm("telegram.resolve_dead_letter"):
        raise PermissionDenied("Telegram dead-letter permission required")
    if actor is None and not service_actor.strip():
        raise PermissionDenied("A service actor is required")
    if not reason.strip() or not evidence_reference.strip() or not idempotency_key.strip():
        raise ValidationError("Reason, evidence reference, and idempotency key are required")
    with transaction.atomic():
        inbound = TelegramInboundUpdate.objects.select_for_update().get(pk=inbound_id)
        existing = TelegramDeadLetterDisposition.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing:
            expected_actor_id = actor.pk if actor is not None else None
            expected_service_actor = service_actor if actor is None else ""
            if (
                existing.inbound_update_id != inbound_id
                or existing.outcome != outcome
                or existing.actor_id != expected_actor_id
                or existing.service_actor != expected_service_actor
                or existing.reason != reason
                or existing.evidence_reference != evidence_reference
            ):
                raise ValidationError("Dead-letter idempotency key conflict")
            return existing
        if inbound.state != TelegramInboundUpdate.State.DEAD_LETTER:
            raise ValidationError("Only a DEAD_LETTER update can be dispositioned")
        disposition = TelegramDeadLetterDisposition.objects.create(
            inbound_update=inbound,
            outcome=outcome,
            actor=actor,
            service_actor=service_actor if actor is None else "",
            reason=reason,
            evidence_reference=evidence_reference,
            idempotency_key=idempotency_key,
            failure_category=inbound.last_error_category,
            processing_attempts=inbound.processing_attempts,
        )
        inbound.claimed_at = None
        inbound.processing_token = None
        inbound.next_retry_at = None
        if outcome == TelegramDeadLetterDisposition.Outcome.REQUEUE:
            inbound.state = TelegramInboundUpdate.State.PENDING
            # Immutable disposition evidence retains the prior attempt count/category.
            # A requeue begins a fresh bounded retry cycle instead of immediately dead-lettering.
            inbound.processing_attempts = 0
            inbound.save()
            transaction.on_commit(lambda: dispatch_inbound_update(inbound))
        elif outcome == TelegramDeadLetterDisposition.Outcome.DISPOSE:
            inbound.state = TelegramInboundUpdate.State.IGNORED
            inbound.processed_at = timezone.now()
            inbound.save()
        else:
            raise ValidationError("Unsupported dead-letter outcome")
        return disposition


def membership_semantics(status: str, is_member: bool | None = None) -> tuple[bool, bool, bool]:
    """Return (present, admin, owner) using Telegram's restricted semantics."""
    owner = status == "creator"
    admin = owner or status == "administrator"
    present = admin or status == "member" or (status == "restricted" and is_member is True)
    return present, admin, owner


class InvalidWebhook(ValueError):
    pass


def verify_webhook_secret(supplied: str | None) -> None:
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected or supplied is None or not secrets.compare_digest(supplied, expected):
        raise InvalidWebhook("Invalid Telegram webhook credentials")


def persist_inbound_update(
    payload: dict[str, Any], *, authenticated: bool = False
) -> tuple[TelegramInboundUpdate, bool]:
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        raise InvalidWebhook("Invalid Telegram update_id")
    expires_at = timezone.now() + timedelta(seconds=settings.TELEGRAM_INGRESS_RETENTION_SECONDS)
    try:
        with transaction.atomic():
            inbound, created = TelegramInboundUpdate.objects.get_or_create(
                update_id=update_id,
                defaults={
                    "payload": payload,
                    "payload_expires_at": expires_at,
                    "webhook_authenticated_at": timezone.now() if authenticated else None,
                },
            )
    except IntegrityError:
        inbound = TelegramInboundUpdate.objects.get(update_id=update_id)
        created = False
    return inbound, created


def dispatch_inbound_update(inbound: TelegramInboundUpdate) -> None:
    from apps.telegram.tasks import process_telegram_update

    TelegramInboundUpdate.objects.filter(pk=inbound.pk).update(
        dispatch_attempts=F("dispatch_attempts") + 1
    )
    process_telegram_update.delay(inbound.pk)
    TelegramInboundUpdate.objects.filter(pk=inbound.pk).update(
        dispatched_at=timezone.now(), last_error_category=""
    )


def mark_dispatch_failed(inbound: TelegramInboundUpdate, error: Exception) -> None:
    TelegramInboundUpdate.objects.filter(pk=inbound.pk).update(
        last_error_category=type(error).__name__[:100]
    )
    logger.error(
        "telegram_ingress_dispatch_failed",
        inbound_update_id=inbound.pk,
        telegram_update_id=inbound.update_id,
        error_category=type(error).__name__,
    )


def process_inbound_update(inbound_id: int) -> None:
    token = _claim_inbound_attempt(inbound_id)
    if token is None:
        return
    try:
        _process_claimed_inbound(inbound_id, token)
    except Exception as exc:
        record_processing_failure(inbound_id, exc, token=token)
        raise


def _claim_inbound_attempt(inbound_id: int) -> uuid.UUID | None:
    """Durably count an attempt before event effects begin."""
    now = timezone.now()
    stale_before = now - timedelta(seconds=settings.TELEGRAM_INGRESS_PROCESSING_LEASE_SECONDS)
    with transaction.atomic():
        inbound = TelegramInboundUpdate.objects.select_for_update().get(pk=inbound_id)
        if inbound.state in TERMINAL_INGRESS_STATES:
            return None
        if inbound.processing_attempts >= settings.TELEGRAM_INGRESS_MAX_PROCESSING_ATTEMPTS:
            inbound.state = TelegramInboundUpdate.State.DEAD_LETTER
            inbound.claimed_at = None
            inbound.processing_token = None
            inbound.next_retry_at = None
            inbound.last_error_category = "processing_attempt_limit"
            inbound.save()
            return None
        if (
            inbound.state == TelegramInboundUpdate.State.PROCESSING
            and inbound.claimed_at is not None
            and inbound.claimed_at > stale_before
        ):
            return None
        if (
            inbound.state == TelegramInboundUpdate.State.FAILED
            and inbound.next_retry_at is not None
            and inbound.next_retry_at > now
        ):
            return None
        token = uuid.uuid4()
        inbound.state = TelegramInboundUpdate.State.PROCESSING
        inbound.claimed_at = now
        inbound.processing_token = token
        inbound.processing_attempts += 1
        inbound.save(
            update_fields=(
                "state",
                "claimed_at",
                "processing_token",
                "processing_attempts",
                "updated_at",
            )
        )
        return token


@transaction.atomic
def _process_claimed_inbound(inbound_id: int, token: uuid.UUID) -> None:
    inbound = TelegramInboundUpdate.objects.select_for_update().get(pk=inbound_id)
    if inbound.state != TelegramInboundUpdate.State.PROCESSING or inbound.processing_token != token:
        return
    from apps.notifications.bot_admin import handle_private_control

    consumed = handle_private_control(inbound)
    event = None if consumed else normalize_update(inbound.payload)
    inbound.state = (
        TelegramInboundUpdate.State.IGNORED
        if event is None and not consumed
        else TelegramInboundUpdate.State.PROCESSED
    )
    if event is not None:
        _apply_event(event, telegram_update_id=inbound.update_id)
    inbound.processed_at = timezone.now()
    inbound.last_error_category = ""
    inbound.next_retry_at = None
    inbound.claimed_at = None
    inbound.processing_token = None
    inbound.save()
    logger.info(
        "telegram_update_processed",
        inbound_update_id=inbound.pk,
        telegram_update_id=inbound.update_id,
        outcome=inbound.state,
    )


def record_processing_failure(
    inbound_id: int, error: Exception, *, token: uuid.UUID | None = None
) -> None:
    with transaction.atomic():
        inbound = TelegramInboundUpdate.objects.select_for_update().get(pk=inbound_id)
        if token is not None and (
            inbound.state != TelegramInboundUpdate.State.PROCESSING
            or inbound.processing_token != token
        ):
            return
        max_attempts = settings.TELEGRAM_INGRESS_MAX_PROCESSING_ATTEMPTS
        terminal = inbound.processing_attempts >= max_attempts
        delay = min(
            settings.TELEGRAM_INGRESS_RETRY_MAX_SECONDS,
            settings.TELEGRAM_INGRESS_RETRY_BASE_SECONDS
            * (2 ** max(inbound.processing_attempts - 1, 0)),
        )
        inbound.state = (
            TelegramInboundUpdate.State.DEAD_LETTER
            if terminal
            else TelegramInboundUpdate.State.FAILED
        )
        inbound.next_retry_at = None if terminal else timezone.now() + timedelta(seconds=delay)
        inbound.claimed_at = None
        inbound.processing_token = None
        inbound.last_error_category = type(error).__name__[:100]
        inbound.save()
    logger.error(
        "telegram_update_processing_failed",
        inbound_update_id=inbound_id,
        error_category=type(error).__name__,
        processing_attempt=inbound.processing_attempts,
        terminal=terminal,
    )


def redrive_retryable_inbound_updates(*, limit: int | None = None) -> int:
    """Claim a bounded batch with SKIP LOCKED, then dispatch after committing."""
    now = timezone.now()
    stale_before = now - timedelta(seconds=settings.TELEGRAM_INGRESS_PROCESSING_LEASE_SECONDS)
    batch = limit or settings.TELEGRAM_INGRESS_REDRIVE_BATCH_SIZE
    with transaction.atomic():
        TelegramInboundUpdate.objects.filter(
            state=TelegramInboundUpdate.State.PROCESSING,
            claimed_at__lte=stale_before,
            processing_attempts__gte=settings.TELEGRAM_INGRESS_MAX_PROCESSING_ATTEMPTS,
        ).update(
            state=TelegramInboundUpdate.State.DEAD_LETTER,
            claimed_at=None,
            processing_token=None,
            next_retry_at=None,
            last_error_category="processing_attempt_limit",
        )
        rows = list(
            TelegramInboundUpdate.objects.select_for_update(skip_locked=True)
            .filter(
                models.Q(
                    state=TelegramInboundUpdate.State.PENDING,
                    received_at__lte=stale_before,
                )
                & (models.Q(claimed_at__isnull=True) | models.Q(claimed_at__lte=stale_before))
                | models.Q(state=TelegramInboundUpdate.State.FAILED, next_retry_at__lte=now)
                | models.Q(
                    state=TelegramInboundUpdate.State.PROCESSING, claimed_at__lte=stale_before
                )
            )
            .order_by("received_at", "pk")[:batch]
        )
        ids = [row.pk for row in rows]
        TelegramInboundUpdate.objects.filter(pk__in=ids).update(
            state=TelegramInboundUpdate.State.PENDING,
            claimed_at=now,
            next_retry_at=None,
            processing_token=None,
        )
    dispatched = 0
    for inbound_id in ids:
        inbound = TelegramInboundUpdate.objects.get(pk=inbound_id)
        try:
            dispatch_inbound_update(inbound)
            dispatched += 1
        except Exception as exc:
            mark_dispatch_failed(inbound, exc)
    return dispatched


def set_group_monitoring(group: TelegramGroup, *, enabled: bool) -> TelegramGroup:
    group.is_monitored = enabled
    group.save(update_fields=("is_monitored", "updated_at"))
    return group


def purge_expired_ingress_payloads() -> int:
    """Remove sensitive raw payloads while retaining dedupe/audit metadata."""
    return (
        TelegramInboundUpdate.objects.filter(
            payload_expires_at__lte=timezone.now(),
            state__in=(
                TelegramInboundUpdate.State.PROCESSED,
                TelegramInboundUpdate.State.IGNORED,
                TelegramInboundUpdate.State.DEAD_LETTER,
            ),
        )
        .exclude(payload={})
        .update(payload={})
    )


@transaction.atomic
def resolve_telegram_player(
    profile: TelegramProfile,
    source: Player.FirstSeenSource,
    *,
    observed_at: datetime | None = None,
    private_contact: bool = False,
    mini_app: bool = False,
    start_parameter: str | None = None,
    telegram_update_id: int | None = None,
) -> PlayerResolution:
    _lock_telegram_user(profile.telegram_user_id)
    resolution = resolve_player(
        profile,
        source,
        observed_at=observed_at,
        private_contact=private_contact,
        mini_app=mini_app,
        start_parameter=start_parameter,
        telegram_update_id=telegram_update_id,
    )
    TelegramUserMembership.objects.filter(
        telegram_user_id=profile.telegram_user_id, player__isnull=True
    ).update(player=resolution.player)
    TelegramGroupCandidate.objects.filter(
        telegram_user_id=profile.telegram_user_id, player__isnull=True
    ).update(player=resolution.player)
    return resolution


def _lock_telegram_user(telegram_user_id: int) -> None:
    """Serialize only identity/membership reconciliation for one Telegram user."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [telegram_user_id])


def _apply_event(event: object, *, telegram_update_id: int) -> None:
    if isinstance(event, BotStarted):
        resolution = resolve_telegram_player(
            event.user,
            Player.FirstSeenSource.BOT_START,
            observed_at=event.observed_at,
            private_contact=True,
            start_parameter=event.parameter,
            telegram_update_id=telegram_update_id,
        )
        from apps.referrals.services import attribute_start

        attribute_start(resolution.player, event.parameter, at=event.observed_at)
        from apps.telegram.welcome import queue_welcome

        queue_welcome(resolution.player)
    elif isinstance(event, TelegramMessageObserved):
        _observe_message(event, telegram_update_id)
    elif isinstance(event, BotMembershipChanged):
        _observe_bot_membership(event, telegram_update_id)
    elif isinstance(event, UserMembershipChanged):
        _observe_user_membership(event, telegram_update_id)


def _upsert_group(
    chat: dict[str, Any], observed_at: datetime, telegram_update_id: int
) -> TelegramGroup:
    chat_id = int(chat["id"])
    chat_type = str(chat["type"])
    if chat_type not in TelegramGroup.ChatType.values:
        raise ValueError("Unsupported Telegram group type")
    group, created = TelegramGroup.objects.get_or_create(
        telegram_chat_id=chat_id,
        defaults={
            "chat_type": chat_type,
            "title": str(chat.get("title", "")),
            "username": str(chat.get("username", "")),
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
            "metadata_last_update_id": telegram_update_id,
        },
    )
    group = TelegramGroup.objects.select_for_update().get(pk=group.pk)
    changed: list[str] = []
    metadata_is_newer = (
        group.metadata_last_update_id is None or telegram_update_id > group.metadata_last_update_id
    )
    for field, value in (
        ("chat_type", chat_type),
        ("title", chat.get("title")),
        ("username", chat.get("username")),
    ):
        current_value = getattr(group, field)
        if (
            value is not None
            and str(value) != current_value
            and (metadata_is_newer or not current_value)
        ):
            setattr(group, field, str(value))
            changed.append(field)
    if observed_at > group.last_seen_at:
        group.last_seen_at = observed_at
        changed.append("last_seen_at")
    if metadata_is_newer:
        group.metadata_last_update_id = telegram_update_id
        changed.append("metadata_last_update_id")
    if changed:
        group.save(update_fields=(*changed, "updated_at"))
    if created:
        logger.info("telegram_group_observed", telegram_group_id=group.pk)
    return group


def _observe_message(event: TelegramMessageObserved, telegram_update_id: int) -> None:
    if event.chat_type not in TelegramGroup.ChatType.values:
        return
    group = _upsert_group(event.chat, event.observed_at, telegram_update_id)
    if not event.sender or event.sender_is_bot:
        return
    command = (event.text or "").split(maxsplit=1)
    expected_command = f"/start@{settings.TELEGRAM_BOT_USERNAME.lstrip('@')}".casefold()
    is_creator_control = (
        len(command) == 2
        and command[0].casefold() == expected_command
        and bool(settings.TELEGRAM_BOT_USERNAME)
    )
    if not group.is_monitored:
        if not is_creator_control:
            return
        from apps.network.services import resolve_intent_creator

        if resolve_intent_creator(command[1], event.sender.telegram_user_id) is None:
            return
    if group.is_monitored or is_creator_control:
        resolution = resolve_telegram_player(
            event.sender,
            Player.FirstSeenSource.GROUP_ACTIVITY,
            observed_at=event.observed_at,
            telegram_update_id=telegram_update_id,
        )
        if is_creator_control:
            from apps.network.services import consume_registration_token

            consume_registration_token(
                command[1], resolution.player, group, observed_at=event.observed_at
            )
            return
        from apps.clans.models import ClanSeason
        from apps.network.models import CreatorGroupRegistration

        clan_candidates = ClanSeason.objects.filter(
            clan__telegram_group=group,
            created_at__lte=event.observed_at,
            season__starts_at__lte=event.observed_at,
            season__ends_at__gt=event.observed_at,
        )
        creator = CreatorGroupRegistration.objects.filter(group=group).first()
        from apps.network.services import registration_reward_eligible_at

        creator_owned = bool(
            creator and registration_reward_eligible_at(creator, event.observed_at)
        )
        # Impossible dual-live ownership fails closed rather than double-crediting.
        from apps.clans.activity import context_owned_at

        clan_season = next(
            (
                candidate
                for candidate in clan_candidates.select_related("clan__telegram_group")
                if context_owned_at(candidate, event.observed_at)
            ),
            None,
        )
        if clan_season is not None and creator_owned:
            logger.error("telegram_dual_live_activity_context", telegram_group_id=group.pk)
            return
        if clan_season is not None:
            from apps.clans.activity import record_clan_activity

            record_clan_activity(
                clan_season=clan_season,
                sender=resolution.player,
                message_id=event.message_id,
                update_id=telegram_update_id,
                text=event.text,
                observed_at=event.observed_at,
            )
            return
        registration = creator if creator_owned else None
        if registration is None:
            return
        from apps.network.services import record_activity

        record_activity(
            registration=registration,
            sender=resolution.player,
            message_id=event.message_id,
            update_id=telegram_update_id,
            text=event.text,
            observed_at=event.observed_at,
            sender_is_bot=event.sender_is_bot,
        )


def _observe_bot_membership(event: BotMembershipChanged, telegram_update_id: int) -> None:
    expected_bot_id = settings.TELEGRAM_BOT_ID
    if expected_bot_id is not None and event.bot_user_id != expected_bot_id:
        logger.warning("telegram_bot_identity_mismatch")
        return
    group = _upsert_group(event.chat, event.observed_at, telegram_update_id)
    if event.actor_telegram_user_id is not None:
        candidate, _ = TelegramGroupCandidate.objects.get_or_create(
            group=group,
            telegram_user_id=event.actor_telegram_user_id,
            defaults={
                "player": Player.objects.filter(
                    telegram_user_id=event.actor_telegram_user_id
                ).first(),
                "last_update_id": telegram_update_id,
                "observed_at": event.observed_at,
            },
        )
        if telegram_update_id > candidate.last_update_id:
            candidate.last_update_id = telegram_update_id
            candidate.observed_at = event.observed_at
            candidate.save(update_fields=("last_update_id", "observed_at"))
    if (
        group.bot_membership_last_update_id is not None
        and telegram_update_id <= group.bot_membership_last_update_id
    ):
        return
    group.bot_membership_status = event.status
    group.bot_is_admin = event.status == "administrator"
    group.bot_can_invite_users = group.bot_is_admin and event.can_invite_users
    group.bot_membership_last_update_id = telegram_update_id
    group.save(
        update_fields=(
            "bot_membership_status",
            "bot_is_admin",
            "bot_can_invite_users",
            "bot_membership_last_update_id",
            "updated_at",
        )
    )
    from apps.clans.governance import observe_bot_admin_state
    from apps.clans.models import ClanSeason

    clan_season_id = (
        ClanSeason.objects.filter(
            clan__telegram_group=group,
            state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
            season__status="ACTIVE",
            season__starts_at__lte=event.observed_at,
            season__ends_at__gt=event.observed_at,
        )
        .order_by("-season__starts_at", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    if clan_season_id is not None:
        observe_bot_admin_state(
            clan_season_id,
            is_admin=group.bot_is_admin and group.bot_can_invite_users,
            authoritative=True,
            whole_second_update=True,
            observed_at=event.observed_at,
        )
    if not group.bot_is_admin:
        from apps.network.models import CreatorGroupRegistration
        from apps.network.services import deactivate_registration_at

        registration_id = (
            CreatorGroupRegistration.objects.filter(group=group)
            .values_list("pk", flat=True)
            .first()
        )
        if registration_id is not None:
            deactivate_registration_at(
                registration_id, at=event.observed_at, source="BOT_ADMIN_LOST"
            )


def _observe_user_membership(event: UserMembershipChanged, telegram_update_id: int) -> None:
    group = _upsert_group(event.chat, event.observed_at, telegram_update_id)
    _lock_telegram_user(event.telegram_user_id)
    player = Player.objects.filter(telegram_user_id=event.telegram_user_id).first()
    present, is_admin, is_owner = membership_semantics(event.status, event.is_member)
    membership, created = TelegramUserMembership.objects.select_for_update().get_or_create(
        group=group,
        telegram_user_id=event.telegram_user_id,
        defaults={
            "player": player,
            "status": event.status,
            "is_member": event.is_member,
            "previous_status": event.previous_status,
            "actor_telegram_user_id": event.actor_telegram_user_id,
            "is_admin": is_admin,
            "is_owner": is_owner,
            "observed_at": event.observed_at,
            "last_update_id": telegram_update_id,
        },
    )
    if not created and telegram_update_id <= membership.last_update_id:
        return
    if not created:
        membership.player = player
        membership.status = event.status
        membership.is_member = event.is_member
        membership.previous_status = event.previous_status
        membership.actor_telegram_user_id = event.actor_telegram_user_id
        membership.is_admin = is_admin
        membership.is_owner = is_owner
        membership.observed_at = event.observed_at
        membership.last_update_id = telegram_update_id
        membership.save(
            update_fields=(
                "player",
                "status",
                "is_member",
                "previous_status",
                "actor_telegram_user_id",
                "is_admin",
                "is_owner",
                "observed_at",
                "last_update_id",
                "updated_at",
            )
        )
    if player is not None:
        from apps.clans.models import ClanSeason
        from apps.clans.recovery import observe_captain_authority

        captain_cs_id = (
            ClanSeason.objects.filter(
                clan__telegram_group=group,
                captain=player,
                state__in=[ClanSeason.State.ACTIVE, ClanSeason.State.RECOVERY],
                season__status="ACTIVE",
                season__starts_at__lte=event.observed_at,
                season__ends_at__gt=event.observed_at,
            )
            .values_list("pk", flat=True)
            .first()
        )
        if captain_cs_id is not None:
            observe_captain_authority(
                captain_cs_id,
                has_authority=is_admin,
                authoritative=True,
                whole_second_update=True,
                observed_at=event.observed_at,
            )
        from apps.clans.services import reconcile_telegram_membership

        reconcile_telegram_membership(
            player=player,
            group=group,
            present=present,
            telegram_update_id=telegram_update_id,
            observed_at=event.observed_at,
            telegram_actor_user_id=event.actor_telegram_user_id,
        )
    try:
        registration = group.creator_registration
    except ObjectDoesNotExist:
        return
    if registration.creator.telegram_user_id == event.telegram_user_id and event.status not in {
        "administrator",
        "creator",
    }:
        from apps.network.services import deactivate_registration_at

        deactivate_registration_at(
            registration.pk, at=event.observed_at, source="CREATOR_ADMIN_LOST"
        )
