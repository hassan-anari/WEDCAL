"""Deterministic Tap wire export; run with DJANGO_SETTINGS_MODULE=settings.test (no DB access)."""

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
    schema["paths"] = {
        k: v for k, v in schema["paths"].items() if "/base/v2/" in k or k.endswith("/base/taps")
    }
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
    target = Path(__file__).resolve().parents[1] / "docs/fixtures/tap-v2-openapi.json"
    if "--check" in sys.argv:
        if target.read_text() != export():
            raise SystemExit("Tap OpenAPI fixture drift; run scripts/export_tap_v2_openapi.py")
    else:
        target.write_text(export())
