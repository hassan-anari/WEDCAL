from typing import Any

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from apps.core.middleware import MutationThrottleMiddleware


@pytest.mark.django_db
@override_settings(
    MUTATION_THROTTLE_POLICIES={"/expensive": (1, 60)},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    TRUST_PROXY_CLIENT_IP=False,
)
def test_untrusted_xff_cannot_create_new_throttle_identities(settings: Any) -> None:
    middleware = MutationThrottleMiddleware(lambda _request: HttpResponse(status=204))
    factory = RequestFactory()
    first = middleware(
        factory.post("/expensive", REMOTE_ADDR="192.0.2.1", HTTP_X_FORWARDED_FOR="198.51.100.1")
    )
    second = middleware(
        factory.post("/expensive", REMOTE_ADDR="192.0.2.1", HTTP_X_FORWARDED_FOR="198.51.100.2")
    )
    assert first.status_code == 204
    assert second.status_code == 429


@pytest.mark.django_db
@override_settings(
    MUTATION_THROTTLE_POLICIES={"/expensive": (1, 60)},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    TRUST_PROXY_CLIENT_IP=True,
)
def test_explicit_trusted_proxy_uses_forwarded_client_identity(settings: Any) -> None:
    middleware = MutationThrottleMiddleware(lambda _request: HttpResponse(status=204))
    factory = RequestFactory()
    first = middleware(
        factory.post("/expensive", REMOTE_ADDR="192.0.2.1", HTTP_X_FORWARDED_FOR="198.51.100.1")
    )
    second = middleware(
        factory.post("/expensive", REMOTE_ADDR="192.0.2.1", HTTP_X_FORWARDED_FOR="198.51.100.2")
    )
    assert first.status_code == second.status_code == 204
