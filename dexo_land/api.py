import structlog
from django.db import InterfaceError, OperationalError
from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError

from apps.arena.api import router as arena_router
from apps.base_tap.api import router as base_tap_router
from apps.base_tap.v2 import TapError
from apps.clans.api import router as clans_router
from apps.content.api import router as content_router
from apps.core.api_errors import IntegrationError, IntegrationErrorOutput
from apps.core.b3 import B3Error
from apps.daily.api import router as daily_router
from apps.game_config.services import GameConfigUnavailable
from apps.missions.api import router as missions_router
from apps.moderation.api import router as moderation_router
from apps.network.api import router as network_router
from apps.notifications.api import router as notifications_router
from apps.players.models import Player
from apps.players.services import reward_gameplay_allowed
from apps.profile_collection.api import router as profile_router
from apps.referrals.api import router as referrals_router
from apps.rewards.api import bootstrap_router, rewards_router
from apps.rewards.services import IdempotencyConflict, InvalidReward
from apps.seasons.barrier import SeasonFinalizationEstablished
from apps.seasons.services import NoActiveSeason, OutsideEconomicWindow, SeasonNotEligible
from apps.telegram.api import auth_router, webhook_router
from apps.typing_game.api import router as typing_router

api = NinjaAPI(title="Dexo Land API", version="1.0.0", urls_namespace="api-v1")


@api.exception_handler(B3Error)
def b3_error(request: HttpRequest, exc: B3Error) -> HttpResponse:
    response = api.create_response(
        request,
        {
            "code": exc.code,
            "detail": exc.code.replace("_", " ").lower(),
            "retryable": exc.status in {429, 503},
            "retry_after_seconds": exc.retry_after,
        },
        status=exc.status,
    )
    response["Cache-Control"] = "private, no-store"
    if exc.retry_after:
        response["Retry-After"] = str(exc.retry_after)
    return response


@api.exception_handler(TapError)
def tap_error(request: HttpRequest, exc: TapError) -> HttpResponse:
    structlog.get_logger(__name__).warning("tap_request_rejected", code=exc.code, status=exc.status)
    response = api.create_response(
        request,
        {
            "code": exc.code,
            "detail": exc.code.replace("_", " ").lower(),
            "retryable": exc.retryable,
            "retry_after_ms": exc.retry_after_ms,
            "snapshot": None,
        },
        status=exc.status,
    )
    response["Cache-Control"] = "private, no-store"
    if exc.status == 429:
        response["Retry-After"] = str(max(1, (exc.retry_after_ms or 1000) // 1000))
    return response


@api.exception_handler(SeasonFinalizationEstablished)
def tap_season_closed(request: HttpRequest, exc: SeasonFinalizationEstablished) -> HttpResponse:
    return tap_error(request, TapError("SEASON_FINALIZED"))


@api.exception_handler(OperationalError)
@api.exception_handler(InterfaceError)
def database_unavailable(request: HttpRequest, exc: Exception) -> HttpResponse:
    return tap_error(request, TapError("TEMPORARY_UNAVAILABLE", 503, retryable=True))


@api.exception_handler(IntegrationError)
def integration_error(request: HttpRequest, exc: IntegrationError) -> HttpResponse:
    if request.path.startswith("/api/v1/base/v2/"):
        return tap_error(
            request,
            TapError(
                "TEMPORARY_UNAVAILABLE" if exc.code == "INITIALIZATION_UNAVAILABLE" else exc.code,
                exc.status_code,
                retryable=exc.status_code == 503,
            ),
        )
    body = IntegrationErrorOutput(
        detail=exc.message, code=exc.code, request_id=request.META.get("DEXO_REQUEST_ID", "")
    )
    response = api.create_response(request, body.model_dump(), status=exc.status_code)
    response["Cache-Control"] = "private, no-store"
    return response


@api.exception_handler(NoActiveSeason)
@api.exception_handler(SeasonNotEligible)
@api.exception_handler(OutsideEconomicWindow)
def reward_season_unavailable(request: HttpRequest, exc: Exception) -> HttpResponse:
    if request.path.startswith("/api/v1/base/v2/"):
        return tap_error(request, TapError("SEASON_UNAVAILABLE"))
    return b3_error(request, B3Error("SEASON_UNAVAILABLE"))


@api.exception_handler(InvalidReward)
def reward_unavailable(request: HttpRequest, exc: InvalidReward) -> HttpResponse:
    player = getattr(request, "auth", None)
    blocked = isinstance(player, Player) and not reward_gameplay_allowed(player)
    return b3_error(
        request,
        B3Error("PLAYER_RESTRICTED" if blocked else "REWARD_UNAVAILABLE", 403 if blocked else 409),
    )


@api.exception_handler(IdempotencyConflict)
def conflicting_reward(request: HttpRequest, exc: IdempotencyConflict) -> HttpResponse:
    return b3_error(request, B3Error("IDEMPOTENCY_CONFLICT"))


@api.exception_handler(GameConfigUnavailable)
def configuration_unavailable(request: HttpRequest, exc: GameConfigUnavailable) -> HttpResponse:
    if request.path.startswith("/api/v1/base/v2/"):
        return tap_error(request, TapError("CONFIG_UNAVAILABLE", 503, retryable=True))
    return integration_error(
        request,
        IntegrationError(
            503,
            "INITIALIZATION_UNAVAILABLE",
            "Application initialization is temporarily unavailable",
        ),
    )


@api.exception_handler(ValidationError)
def request_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    if request.path.startswith("/api/v1/base/v2/"):
        return tap_error(request, TapError("REQUEST_INVALID", 422))
    if request.path.startswith(
        (
            "/api/v1/daily",
            "/api/v1/missions",
            "/api/v1/typing",
            "/api/v1/network",
            "/api/v1/referrals",
            "/api/v1/content",
            "/api/v1/clans",
            "/api/v1/rewards/history",
        )
    ):
        return b3_error(request, B3Error("REQUEST_INVALID", 422))
    if request.path.rstrip("/") == "/api/v1/auth/telegram":
        # Do not echo credentials or Pydantic input values into client errors.
        return integration_error(
            request,
            IntegrationError(422, "REQUEST_INVALID", "A string init_data field is required"),
        )
    return api.create_response(request, {"detail": exc.errors}, status=422)


@api.exception_handler(HttpError)
def http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
    if request.path.startswith("/api/v1/clans"):
        return b3_error(
            request,
            B3Error(
                "CLAN_NOT_FOUND" if exc.status_code == 404 else "CLAN_REQUEST_REJECTED",
                exc.status_code,
            ),
        )
    if request.path.startswith("/api/v1/base/v2/") and exc.status_code == 400:
        return tap_error(request, TapError("REQUEST_INVALID", 422))
    if request.path.rstrip("/") == "/api/v1/auth/telegram" and exc.status_code == 400:
        return integration_error(
            request, IntegrationError(400, "REQUEST_INVALID", "A JSON request body is required")
        )
    return api.create_response(request, {"detail": exc.message}, status=exc.status_code)


api.add_router("/auth", auth_router)
api.add_router("/telegram", webhook_router)
api.add_router("/bootstrap", bootstrap_router)
api.add_router("/rewards", rewards_router)
api.add_router("/daily", daily_router)
api.add_router("/missions", missions_router)
api.add_router("/typing", typing_router)
api.add_router("/referrals", referrals_router)
api.add_router("/network", network_router)
api.add_router("/base", base_tap_router)
api.add_router("/profile", profile_router)
api.add_router("/clans", clans_router)
api.add_router("/moderation", moderation_router)
api.add_router("/arena", arena_router)
api.add_router("/notifications", notifications_router)

api.add_router("/content", content_router)
