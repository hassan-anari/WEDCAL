import hashlib
import io
import warnings
from typing import Any
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from PIL import Image, ImageOps

from apps.content import storage
from apps.content.models import BannerAsset, PromotionalBanner
from apps.notifications.models import OperatorAuditEvent


def validate_destination(url: str) -> None:
    if not url:
        return
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
            or parts.fragment
            or "%" in parts.path
            or "\\" in url
            or any(ord(c) < 33 for c in url)
            or parts.path.startswith("//")
            or parts.hostname.lower() not in settings.BANNER_ALLOWED_LINK_HOSTS
        ):
            raise ValueError("unsafe_destination")
        # Do not accept open-redirect parameters. Only Telegram's documented launch keys.
        if parts.query and (
            parts.hostname.lower() != "t.me"
            or set(parse_qs(parts.query)) - {"start", "startapp", "startgroup"}
        ):
            raise ValueError("redirect_query")
    except (ValueError, TypeError) as exc:
        raise ValidationError("Destination must use an approved HTTPS host and safe path") from exc


def encode_image(upload: Any) -> bytes:
    raw = upload.read(settings.BANNER_MAX_BYTES + 1)
    if not raw or len(raw) > settings.BANNER_MAX_BYTES:
        raise ValidationError("Image exceeds the configured size limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if (
                    original.format not in {"JPEG", "PNG", "WEBP"}
                    or getattr(original, "n_frames", 1) != 1
                ):
                    raise ValueError("image_type")
                width, height = original.size
                if width * 13 != height * 60 or not (600 <= width <= 2400 and 130 <= height <= 520):
                    raise ValueError("image_dimensions")
                original.load()
                image = ImageOps.exif_transpose(original).convert("RGB").resize((1200, 260))
                output = io.BytesIO()
                image.save(output, format="WEBP", quality=82, method=4)
                encoded = output.getvalue()
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValidationError(
            "Upload a real static JPEG/PNG/WebP with a 60:13 aspect ratio"
        ) from exc
    if len(encoded) > settings.BANNER_MAX_BYTES:
        raise ValidationError("Encoded image exceeds size limit")
    return encoded


def upload_asset(upload: Any, actor: Any) -> BannerAsset:
    if not actor.has_perm("content.add_bannerasset"):
        raise PermissionDenied
    if not storage.configured():
        raise ValidationError("Banner storage is not configured")
    encoded = encode_image(upload)
    digest = hashlib.sha256(encoded).hexdigest()
    asset = BannerAsset(
        storage_key=f"banners/{digest}.webp",
        digest=digest,
        width=1200,
        height=260,
        byte_size=len(encoded),
        approved=True,
        created_by=actor,
    )
    existing = BannerAsset.objects.filter(storage_key=asset.storage_key).first()
    if existing:
        return existing
    storage.put(asset.storage_key, encoded)
    asset.save()
    return asset


def banner_state(row: PromotionalBanner) -> dict[str, Any]:
    return {
        "enabled": row.enabled,
        "placement": row.placement,
        "asset_id": str(row.asset_id),
        "alt_text": row.alt_text,
        "destination": row.destination,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "priority": row.priority,
        "deactivated_at": row.deactivated_at.isoformat() if row.deactivated_at else None,
    }


@transaction.atomic
def save_banner(
    row: PromotionalBanner, actor: Any, *, deactivate: bool = False
) -> PromotionalBanner:
    if not actor.has_perm(
        "content.change_promotionalbanner"
        if PromotionalBanner.objects.filter(pk=row.pk).exists()
        else "content.add_promotionalbanner"
    ):
        raise PermissionDenied
    old = PromotionalBanner.objects.select_for_update().filter(pk=row.pk).first()
    if (row.enabled or old and old.enabled) and not actor.has_perm(
        "content.publish_promotionalbanner"
    ):
        raise PermissionDenied
    if deactivate:
        row.enabled = False
        row.deactivated_at = timezone.now()
    row.operator = actor
    row.full_clean()
    row.save()
    action = (
        "DEACTIVATE"
        if deactivate
        else "PUBLISH"
        if old is None and row.enabled
        else "ENABLE"
        if row.enabled and (old is None or not old.enabled)
        else "DISABLE"
        if old and old.enabled and not row.enabled
        else "UPDATE"
        if old
        else "CREATE"
    )
    OperatorAuditEvent.objects.create(
        actor=actor,
        action=f"BANNER_{action}",
        target_type="PromotionalBanner",
        target_id=str(row.pk),
        reason="Banner management",
        before_state=banner_state(old) if old else {},
        after_state=banner_state(row),
    )
    return row


def effective_banner(placement: str) -> dict[str, Any]:
    if not storage.configured():
        return {"placement": placement, "banner": None}
    now = timezone.now()
    rows = (
        PromotionalBanner.objects.select_related("asset")
        .filter(
            placement=placement, enabled=True, deactivated_at__isnull=True, asset__approved=True
        )
        .filter(
            Q(starts_at__isnull=True) | Q(starts_at__lte=now),
            Q(ends_at__isnull=True) | Q(ends_at__gt=now),
        )
        .order_by("-priority", "created_at", "id")
    )
    for row in rows.iterator():
        try:
            row.full_clean()
        except ValidationError:
            continue
        assert row.asset is not None
        url = (
            f"{settings.BANNER_ASSET_ORIGIN.rstrip('/')}/api/v1/content/banner-assets/"
            f"{row.asset_id}/{row.asset.digest}.webp"
        )
        return {
            "placement": placement,
            "banner": {
                "id": row.id,
                "image_url": url,
                "alt_text": row.alt_text,
                "destination": row.destination or None,
                "destination_type": (
                    "TELEGRAM" if urlsplit(row.destination).hostname == "t.me" else "HTTPS"
                )
                if row.destination
                else None,
                "width": row.asset.width,
                "height": row.asset.height,
                "version": row.asset.digest,
            },
        }
    return {"placement": placement, "banner": None}
