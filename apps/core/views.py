from django.http import JsonResponse
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
