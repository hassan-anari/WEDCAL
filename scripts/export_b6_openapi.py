"""Export the current B6 integration contract using the full B5 route inventory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.export_b5_openapi import export  # noqa: E402

if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "docs/fixtures/b6/openapi.json"
    if "--check" in sys.argv:
        if target.read_text() != export():
            raise SystemExit("B6 OpenAPI drift: run scripts/export_b6_openapi.py")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(export())
