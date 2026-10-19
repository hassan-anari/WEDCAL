import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from apps.core.b3 import B3Error
from apps.core.periods import resolve_daily_period
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import ResolvedGameConfig, resolve_effective_game_config
from apps.missions.models import MissionDefinition, PlayerMission, validate_mission_parameters
from apps.players.models import Player
from apps.rewards.services import settle_player_reward
from apps.seasons.services import require_reward_earning_time, resolve_current_season
from apps.telegram.bot_api import TelegramResult, get_chat_member
from apps.typing_game.models import TypingAttempt

logger = structlog.get_logger(__name__)


class MissionUnavailable(B3Error, LookupError):
    def __init__(self, detail: str):
        super().__init__("MISSION_UNAVAILABLE")


class MissionVerificationUnavailable(B3Error, RuntimeError):
    def __init__(self, category: str, retry_after: int | None = None):
        self.category, self.retry_after = category, retry_after
        super().__init__("TELEGRAM_VERIFICATION_UNAVAILABLE", 503, retry_after=retry_after)


@dataclass(frozen=True, slots=True)
class MissionStart:
    progress: PlayerMission
    open_token: str | None
    destination_url: str | None


SEASON_BRIEF_DESTINATION = "https://dexoland.dalexio.com/"


def require_approved_destination(definition: MissionDefinition) -> None:
    """PH-043 owner decision; legacy definitions remain historical, never auto-converted."""
    if definition.key == "read-season-brief" and (
        definition.verification_family != MissionDefinition.Verification.TIMED_EXTERNAL
        or definition.repeat_policy != MissionDefinition.Repeat.ONCE
        or definition.verification_parameters.get("destination_url") != SEASON_BRIEF_DESTINATION
    ):
        raise MissionUnavailable("Publish and activate the approved external Season Brief version")


def _definition(key: str, at: datetime) -> MissionDefinition:
    definition = (
        MissionDefinition.objects.filter(key=key, enabled=True)
        .filter(
            Q(available_from__isnull=True) | Q(available_from__lte=at),
            Q(available_until__isnull=True) | Q(available_until__gt=at),
        )
        .order_by("-version")
        .first()
    )
    if definition is None:
        raise MissionUnavailable("Mission is unavailable")
    return definition


def _require_policy_binding(definition: MissionDefinition, resolved: ResolvedGameConfig) -> None:
    try:
        validate_mission_parameters(
            definition.verification_family, definition.verification_parameters
        )
        rule = resolved.missions.definition(definition.key)
    except (ValidationError, ValueError, LookupError) as exc:
        raise MissionUnavailable("Mission policy is unavailable") from exc
    if (
        definition.verification_family != rule.verification
        or definition.repeat_policy != rule.repeat
    ):
        raise MissionUnavailable("Mission definition does not match effective policy")


def _eligibility(
    definition: MissionDefinition,
    at: datetime,
    timezone_name: str,
    reset_hour: int,
    reset_minute: int,
) -> str:
    if definition.repeat_policy == MissionDefinition.Repeat.DAILY:
        return resolve_daily_period(at, timezone_name, reset_hour, reset_minute).key
    return "definition"


def _mission_period(
    definition: MissionDefinition, at: datetime, resolved: ResolvedGameConfig
) -> str:
    timezone_name = (
        resolved.typing.reset_timezone
        if definition.key == "typing-session"
        else resolved.daily.reset_timezone
    )
    return _eligibility(
        definition, at, timezone_name, resolved.daily.reset_hour, resolved.daily.reset_minute
    )


def _open_token(progress: PlayerMission) -> str:
    identity = (
        f"{progress.player_id}:{progress.definition_id}:{progress.eligibility_period}".encode()
    )
    return hmac.new(settings.SECRET_KEY.encode(), identity, hashlib.sha256).hexdigest()


def list_missions(player: Player, at: datetime) -> list[dict[str, object]]:
    resolved = resolve_effective_game_config(at)
    if not resolved.missions.enabled:
        raise MissionUnavailable("Missions are unavailable in the effective configuration")
    definitions = list(
        MissionDefinition.objects.filter(enabled=True)
        .filter(
            Q(available_from__isnull=True) | Q(available_from__lte=at),
            Q(available_until__isnull=True) | Q(available_until__gt=at),
        )
        .order_by("key", "-version")
    )
    latest: dict[str, MissionDefinition] = {}
    for definition in definitions:
        latest.setdefault(definition.key, definition)
    identities = [
        (
            row.pk,
            _mission_period(row, at, resolved),
        )
        for row in latest.values()
    ]
    progress = {
        (row.definition_id, row.eligibility_period): row
        for row in PlayerMission.objects.filter(
            player=player, definition_id__in=[x[0] for x in identities]
        )
    }
    result = []
    for definition in latest.values():
        if not isinstance(definition.verification_parameters, dict):
            continue
        try:
            _require_policy_binding(definition, resolved)
            rule = resolved.missions.definition(definition.key)
        except (MissionUnavailable, ValueError, LookupError):
            logger.warning("mission_definition_unavailable", definition_id=definition.pk)
            continue  # Quarantine malformed/legacy rows; one definition cannot break Bootstrap.
        period = _mission_period(definition, at, resolved)
        row = progress.get((definition.pk, period))
        try:
            require_approved_destination(definition)
            available = True
        except MissionUnavailable:
            available = False
        result.append(
            {
                "key": definition.key,
                "version": definition.version,
                "title": definition.title,
                "description": definition.description,
                "verification_family": definition.verification_family,
                "repeat_policy": definition.repeat_policy,
                "reward_amount": rule.reward_points,
                "state": row.status if row else ("AVAILABLE" if available else "UNAVAILABLE"),
                "available": available,
                "unavailable_reason": None if available else "PRODUCT_EVIDENCE_PENDING",
                "destination_url": definition.verification_parameters.get("destination_url"),
                "opened_at": row.opened_at if row else None,
                "verified_at": row.verified_at if row else None,
                "claimed_at": row.claimed_at if row else None,
            }
        )
    return result


def start_mission(player: Player, key: str, at: datetime) -> MissionStart:
    resolved = resolve_effective_game_config(at)
    if not resolved.missions.enabled:
        raise MissionUnavailable("Missions are unavailable in the effective configuration")
    definition = _definition(key, at)
    _require_policy_binding(definition, resolved)
    require_approved_destination(definition)
    eligibility = _mission_period(definition, at, resolved)
    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player.pk)
        definition = MissionDefinition.objects.select_for_update().get(pk=definition.pk)
        if not definition.enabled:
            raise MissionUnavailable("Mission was deactivated")
        _require_policy_binding(definition, resolved)
        progress, created = PlayerMission.objects.get_or_create(
            player=player, definition=definition, eligibility_period=eligibility
        )
        token = None
        if (
            definition.verification_family == MissionDefinition.Verification.TIMED_EXTERNAL
            and progress.status == PlayerMission.Status.IN_PROGRESS
        ):
            token = _open_token(progress)
            if created:
                progress.opened_at = at
            progress.open_token_hash = hashlib.sha256(token.encode()).hexdigest()
            progress.save(update_fields=["opened_at", "open_token_hash", "updated_at"])
    logger.info(
        "mission_started",
        player_id=player.pk,
        mission_key=key,
        definition_version=definition.version,
    )
    return MissionStart(
        progress,
        token,
        definition.verification_parameters.get("destination_url")
        if definition.verification_family == MissionDefinition.Verification.TIMED_EXTERNAL
        else None,
    )


def _mark_ready(player: Player, progress_id: int, at: datetime) -> PlayerMission:
    resolved = resolve_effective_game_config(at)
    season = resolve_current_season(at=at)
    assert season is not None
    require_reward_earning_time(season, at)
    with transaction.atomic():
        from apps.seasons.barrier import acquire_economic_write_barrier

        acquire_economic_write_barrier(season.pk)
        player = Player.objects.select_for_update().get(pk=player.pk)
        progress = (
            PlayerMission.objects.select_for_update()
            .select_related("definition")
            .get(pk=progress_id, player=player)
        )
        if progress.status in (PlayerMission.Status.REWARD_READY, PlayerMission.Status.CLAIMED):
            return progress
        if not resolved.missions.enabled or not progress.definition.enabled:
            raise MissionUnavailable("Mission was disabled before verification completed")
        rule = resolved.missions.definition(progress.definition.key)
        _require_policy_binding(progress.definition, resolved)
        progress.status = PlayerMission.Status.REWARD_READY
        progress.season, progress.config_version = (
            season,
            GameConfigVersion.objects.get(pk=resolved.id),
        )
        progress.reward_amount, progress.verified_at = rule.reward_points, at
        progress.save(
            update_fields=[
                "status",
                "season",
                "config_version",
                "reward_amount",
                "verified_at",
                "updated_at",
            ]
        )
    logger.info("mission_verified", player_id=player.pk, mission_key=progress.definition.key)
    return progress


def verify_mission(
    player: Player, key: str, at: datetime, token: str | None = None
) -> PlayerMission:
    resolved = resolve_effective_game_config(at)
    if not resolved.missions.enabled:
        raise MissionUnavailable("Missions are unavailable in the effective configuration")
    definition = _definition(key, at)
    _require_policy_binding(definition, resolved)
    require_approved_destination(definition)
    eligibility = _mission_period(definition, at, resolved)
    progress = (
        PlayerMission.objects.select_related("definition")
        .filter(player=player, definition=definition, eligibility_period=eligibility)
        .first()
    )
    if progress is None:
        progress = start_mission(player, key, at).progress
    if progress.status != PlayerMission.Status.IN_PROGRESS:
        return progress
    if definition.verification_family == MissionDefinition.Verification.TIMED_EXTERNAL:
        if not token or not secrets.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), progress.open_token_hash
        ):
            raise ValueError("Invalid mission open token")
        if (
            progress.opened_at is None
            or (at - progress.opened_at).total_seconds()
            < resolved.missions.timed_external_min_dwell_seconds
        ):
            return progress
    elif definition.verification_family == MissionDefinition.Verification.TELEGRAM:
        chat_id = definition.verification_parameters.get("telegram_chat_id")
        if type(chat_id) is not int:
            raise MissionUnavailable("Telegram target is not configured")
        result: TelegramResult = get_chat_member(chat_id, player.telegram_user_id)
        if not result.ok:
            logger.warning(
                "telegram_mission_verification_unavailable",
                player_id=player.pk,
                mission_key=key,
                category=result.category,
            )
            raise MissionVerificationUnavailable(result.category or "unknown", result.retry_after)
        member = result.result or {}
        user = member.get("user")
        if not isinstance(user, dict) or user.get("id") != player.telegram_user_id:
            raise ValueError("Telegram returned a different user")
        status = member.get("status")
        accepted = status in {"creator", "administrator", "member"} or (
            status == "restricted" and member.get("is_member") is True
        )
        if not accepted:
            return progress
    else:
        if definition.key == "complete-profile":
            from apps.profile_collection.models import PlayerProfile

            if not PlayerProfile.objects.filter(
                player=player, first_reviewed_at__isnull=False
            ).exists():
                return progress
        elif definition.key == "typing-session":
            if not TypingAttempt.objects.filter(
                player=player, reset_period=eligibility, status=TypingAttempt.Status.VALID
            ).exists():
                return progress
        else:
            raise MissionUnavailable("Internal verifier is not implemented")
    return _mark_ready(player, progress.pk, at)


def claim_mission(player: Player, key: str, at: datetime) -> tuple[PlayerMission, bool]:
    resolved_now = resolve_effective_game_config(at)
    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player.pk)
        # Preserve verified obligations even if a definition is later disabled or replaced.
        progress = (
            PlayerMission.objects.select_for_update()
            .select_related("definition")
            .filter(player=player, definition__key=key, status=PlayerMission.Status.REWARD_READY)
            .order_by("verified_at", "pk")
            .first()
        )
        if progress is None:
            definition = _definition(key, at)
            eligibility = _mission_period(definition, at, resolved_now)
            progress = (
                PlayerMission.objects.select_for_update()
                .select_related("definition")
                .filter(player=player, definition=definition, eligibility_period=eligibility)
                .first()
            )
            if progress is None:
                raise B3Error("MISSION_NOT_READY")
        definition = progress.definition
        eligibility = progress.eligibility_period
        if progress.status == PlayerMission.Status.CLAIMED:
            return progress, False
        if (
            progress.status != PlayerMission.Status.REWARD_READY
            or not progress.verified_at
            or not progress.config_version_id
            or not progress.reward_amount
        ):
            raise B3Error("MISSION_NOT_READY")
        from apps.game_config.services import resolve_game_config_version

        resolved = resolve_game_config_version(progress.config_version_id)
        _require_policy_binding(definition, resolved)
        ledger, created = settle_player_reward(
            player=player,
            source_type="MISSION",
            source_id=f"{definition.key}:v{definition.version}:{eligibility}",
            context="MISSION",
            amount=progress.reward_amount,
            idempotency_key=f"mission:{player.pk}:{definition.pk}:{eligibility}",
            occurred_at=progress.verified_at,
            resolved_config=resolved,
        )
        progress.status, progress.claimed_at, progress.reward_ledger = (
            PlayerMission.Status.CLAIMED,
            at,
            ledger,
        )
        progress.save(update_fields=["status", "claimed_at", "reward_ledger", "updated_at"])
    logger.info("mission_claimed", player_id=player.pk, mission_key=key)
    return progress, created
