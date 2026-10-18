"""Export implemented B5 routes and transitive schemas; no speculative contracts."""

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dexo_land.settings.test")
import django  # noqa: E402

django.setup()
from dexo_land.api import api  # noqa: E402


def export() -> str:
    schema = api.get_openapi_schema()
    prefixes = (
        "/api/v1/clans",
        "/api/v1/bootstrap",
        "/api/v1/profile/",
        "/api/v1/arena/",
        "/api/v1/notifications",
        "/api/v1/daily",
        "/api/v1/missions",
        "/api/v1/typing",
        "/api/v1/referrals",
        "/api/v1/network",
        "/api/v1/base/v2/",
        "/api/v1/content/banners/",
        "/api/v1/rewards/state",
        "/api/v1/rewards/history",
    )
    schema["paths"] = {k: v for k, v in schema["paths"].items() if k.startswith(prefixes)}
    needed: set[str] = set()

    def refs(obj: Any) -> None:
        if isinstance(obj, dict):
            if "$ref" in obj and obj["$ref"].startswith("#/components/schemas/"):
                needed.add(obj["$ref"].split("/")[-1])
            for value in obj.values():
                refs(value)
        elif isinstance(obj, list):
            for value in obj:
                refs(value)

    refs(schema["paths"])
    previous = -1
    while len(needed) != previous:
        previous = len(needed)
        for name in list(needed):
            refs(schema["components"]["schemas"][name])
    schema["components"]["schemas"] = {
        k: v for k, v in schema["components"]["schemas"].items() if k in needed
    }
    return json.dumps(schema, indent=2, default=str) + "\n"


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "docs/fixtures/b5/openapi.json"
    if "--check" in sys.argv:
        if target.read_text() != export():
            raise SystemExit("B5 OpenAPI drift: run scripts/export_b5_openapi.py")
    else:
        target.write_text(export())
