from django.conf import settings
from django.http import HttpRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.http import require_GET

from apps.core.health import readiness_status


@require_GET
def live(request: object) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def ready(request: object) -> JsonResponse:
    is_ready, dependencies = readiness_status()
    return JsonResponse(
        {"status": "ready" if is_ready else "unavailable", "dependencies": dependencies},
        status=200 if is_ready else 503,
    )


@require_GET
def metrics(request: HttpRequest) -> JsonResponse | HttpResponseForbidden:
    if request.headers.get("X-Operations-Token") != settings.OPERATIONS_METRICS_TOKEN:
        return HttpResponseForbidden()
    from apps.core.metrics import operational_snapshot

    return JsonResponse(operational_snapshot())
