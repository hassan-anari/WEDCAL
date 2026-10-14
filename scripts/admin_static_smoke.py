"""Run inside the image under app, DEBUG=False and manifest storage; no DB required."""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dexo_land.settings.static_build")
import django  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.staticfiles.storage import staticfiles_storage  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

django.setup()


def main() -> None:
    assert not settings.DEBUG
    assert (
        settings.STORAGES["staticfiles"]["BACKEND"]
        == "whitenoise.storage.CompressedManifestStaticFilesStorage"
    )
    manifest = Path(settings.STATIC_ROOT) / "staticfiles.json"
    assert manifest.stat().st_size > 0
    assert json.loads(manifest.read_text())["paths"]
    assert os.access(settings.STATIC_ROOT, os.W_OK), "app must own static output directory"
    asset = staticfiles_storage.url("admin/css/base.css")
    assert re.fullmatch(r"/static/admin/css/base\.[a-f0-9]+\.css", asset)
    with override_settings(ALLOWED_HOSTS=["testserver"]):
        client = Client()
        response = client.get("/admin/login/")
        assert response.status_code == 200
        assert asset.encode() in response.content
        response = client.get(asset)
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/css")
    print(
        json.dumps(
            {
                "admin_login_status": 200,
                "asset_status": 200,
                "hashed_asset": asset,
                "manifest_bytes": manifest.stat().st_size,
                "uid": os.getuid(),
                "debug": settings.DEBUG,
            }
        )
    )


if __name__ == "__main__":
    main()
