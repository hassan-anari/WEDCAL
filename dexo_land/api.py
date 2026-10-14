from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError

from apps.arena.api import router as arena_router
from apps.base_tap.api import router as base_tap_router
from apps.clans.api import router as clans_router
from apps.core.api_errors import IntegrationError, IntegrationErrorOutput
from apps.daily.api import router as daily_router
from apps.missions.api import router as missions_router
from apps.moderation.api import router as moderation_router
from apps.network.api import router as network_router
from apps.notifications.api import router as notifications_router
from apps.profile_collection.api import router as profile_router
from apps.referrals.api import router as referrals_router
from apps.rewards.api import bootstrap_router, rewards_router
from apps.telegram.api import auth_router, webhook_router
from apps.typing_game.api import router as typing_router

api = NinjaAPI(title="Dexo Land API", version="1.0.0", urls_namespace="api-v1")


@api.exception_handler(IntegrationError)
def integration_error(request: HttpRequest, exc: IntegrationError) -> HttpResponse:
    body = IntegrationErrorOutput(
        detail=exc.message, code=exc.code, request_id=request.META.get("DEXO_REQUEST_ID", "")
    )
    response = api.create_response(request, body.model_dump(), status=exc.status_code)
    response["Cache-Control"] = "private, no-store"
    return response


@api.exception_handler(ValidationError)
def request_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    if request.path.rstrip("/") == "/api/v1/auth/telegram":
        # Do not echo credentials or Pydantic input values into client errors.
        return integration_error(
            request,
            IntegrationError(422, "REQUEST_INVALID", "A string init_data field is required"),
        )
    return api.create_response(request, {"detail": exc.errors}, status=422)


@api.exception_handler(HttpError)
def http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
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
