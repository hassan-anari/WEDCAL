from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from ninja import Router, Schema

from apps.content import storage
from apps.content.models import BannerAsset
from apps.content.services import effective_banner

router = Router(tags=["content"])


class BannerOutput(Schema):
    id: UUID
    image_url: str
    alt_text: str
    destination: str | None
    destination_type: Literal["TELEGRAM", "HTTPS"] | None
    width: int
    height: int
    version: str


class EffectiveBanner(Schema):
    placement: Literal["BASE"]
    banner: BannerOutput | None


@router.get("/banners/{placement}", response=EffectiveBanner)
def banner(request: HttpRequest, placement: Literal["BASE"]) -> dict[str, Any]:
    return effective_banner(placement)


@router.get("/banner-assets/{asset_id}/{digest}.webp", include_in_schema=False)
def asset(request: HttpRequest, asset_id: UUID, digest: str) -> HttpResponse:
    row = BannerAsset.objects.filter(pk=asset_id, digest=digest, approved=True).first()
    if not row or not storage.configured():
        return HttpResponse(status=404)
    try:
        content = storage.get(row.storage_key)
    except Exception:
        return HttpResponse(status=503, headers={"Cache-Control": "no-store"})
    import hashlib

    if hashlib.sha256(content).hexdigest() != row.digest:
        return HttpResponse(status=503, headers={"Cache-Control": "no-store"})
    return HttpResponse(
        content,
        content_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{digest}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )
