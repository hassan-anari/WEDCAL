# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import io
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from PIL import Image

from apps.content.models import PromotionalBanner
from apps.content.services import effective_banner, save_banner, upload_asset, validate_destination
from apps.notifications.models import OperatorAuditEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def content_world(settings, monkeypatch):
    settings.BANNER_S3_BUCKET = "synthetic"
    settings.BANNER_ASSET_ORIGIN = "https://assets.example.test"
    objects = {}
    monkeypatch.setattr(
        "apps.content.storage.put", lambda key, content: objects.update({key: content})
    )
    monkeypatch.setattr("apps.content.storage.get", lambda key: objects[key])
    actor = User.objects.create_superuser("banner-operator", "op@example.test", "synthetic")
    stream = io.BytesIO()
    Image.new("RGB", (1200, 260), "purple").save(stream, format="PNG")
    stream.seek(0)
    asset = upload_asset(stream, actor)
    return actor, asset, objects


def test_disabled_schedule_priority_safe_empty_and_deactivation_audit(content_world, settings):
    actor, asset, _ = content_world
    row = PromotionalBanner(asset=asset, alt_text="Dexo community", operator=actor)
    save_banner(row, actor)
    assert effective_banner("BASE") == {"placement": "BASE", "banner": None}
    row.enabled = True
    row.starts_at = timezone.now() + timedelta(hours=1)
    save_banner(row, actor)
    assert effective_banner("BASE")["banner"] is None
    row.starts_at = timezone.now() - timedelta(hours=1)
    row.destination = "https://t.me/Dalexio_Official"
    save_banner(row, actor)
    active = effective_banner("BASE")
    assert active["banner"]["id"] == row.pk
    row2 = PromotionalBanner(
        asset=asset, alt_text="Priority", enabled=True, priority=5, operator=actor
    )
    save_banner(row2, actor)
    assert effective_banner("BASE")["banner"]["id"] == row2.pk
    save_banner(row2, actor, deactivate=True)
    assert effective_banner("BASE")["banner"]["id"] == row.pk
    row.ends_at = timezone.now() - timedelta(minutes=1)
    save_banner(row, actor)
    assert effective_banner("BASE")["banner"] is None
    assert OperatorAuditEvent.objects.filter(action="BANNER_DEACTIVATE").count() == 1
    settings.BANNER_S3_BUCKET = ""
    assert effective_banner("BASE")["banner"] is None
    if os.getenv("B3_EXPORT_FIXTURES"):
        Path("docs/fixtures/b3/banners.json").write_text(
            json.dumps({"active": active, "empty": effective_banner("BASE")}, default=str, indent=2)
            + "\n"
        )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "http://t.me/a",
        "https://t.me.evil.test/a",
        "https://t.me@evil.test/a",
        "https://t.me/a?url=https://evil.test",
        "https://t.me:444/a",
        "https://t.me/\\evil.test",
        "https://t.me/%0aevil",
        "https://t.me//evil.test",
        "https://t.me/a#javascript:x",
    ],
)
def test_destination_rejects_unsafe_links(url):
    with pytest.raises(ValidationError):
        validate_destination(url)


@pytest.mark.parametrize("kind", ["svg", "html", "size", "ratio", "animated"])
def test_real_image_validation(content_world, kind):
    actor, _, _ = content_world
    raw = io.BytesIO(
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>" if kind == "svg" else b"<html>bad</html>"
    )
    if kind in {"size", "ratio", "animated"}:
        raw = io.BytesIO()
        size = (3000, 650) if kind == "size" else (1200, 300) if kind == "ratio" else (1200, 260)
        image = Image.new("RGB", size, "red")
        image.save(
            raw,
            format="WEBP",
            save_all=kind == "animated",
            append_images=[Image.new("RGB", size, "blue")] if kind == "animated" else [],
        )
        raw.seek(0)
    with pytest.raises(ValidationError):
        upload_asset(raw, actor)


def test_upload_permission_and_no_storage_and_asset_delivery(content_world, client, settings):
    actor, asset, objects = content_world
    response = client.get(f"/api/v1/content/banner-assets/{asset.pk}/{asset.digest}.webp")
    assert response.status_code == 200 and response["Content-Type"] == "image/webp"
    assert (
        response.content == objects[asset.storage_key] and "immutable" in response["Cache-Control"]
    )
    user = User.objects.create_user("no-permission")
    with pytest.raises(PermissionDenied):
        upload_asset(io.BytesIO(), user)
    settings.BANNER_S3_BUCKET = ""
    with pytest.raises(ValidationError):
        upload_asset(io.BytesIO(), actor)


def test_admin_upload_validates_before_save_and_can_publish_new_image(content_world):
    from django.contrib import admin
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import RequestFactory

    from apps.content.admin import BannerAdmin, BannerForm

    actor, _, _ = content_world
    bad = BannerForm(
        data={"placement": "BASE", "enabled": True, "alt_text": "Accessible", "priority": 0},
        files={
            "image_upload": SimpleUploadedFile(
                "bad.png", b"<svg>bad</svg>", content_type="image/png"
            )
        },
    )
    assert not bad.is_valid() and "image_upload" in bad.errors
    stream = io.BytesIO()
    Image.new("RGB", (1200, 260), "blue").save(stream, format="PNG")
    good = BannerForm(
        data={"placement": "BASE", "enabled": True, "alt_text": "Accessible", "priority": 0},
        files={
            "image_upload": SimpleUploadedFile(
                "banner.png", stream.getvalue(), content_type="image/png"
            )
        },
    )
    assert good.is_valid(), good.errors
    request = RequestFactory().post("/admin/content/promotionalbanner/add/")
    request.user = actor
    BannerAdmin(PromotionalBanner, admin.site).save_model(request, good.instance, good, False)
    assert good.instance.enabled and good.instance.asset_id
