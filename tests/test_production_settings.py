import os
import subprocess
import sys


def test_production_settings_reject_missing_secret() -> None:
    environment = os.environ.copy()
    environment.pop("DJANGO_SECRET_KEY", None)
    environment["DJANGO_ALLOWED_HOSTS"] = "example.com"
    result = subprocess.run(
        [sys.executable, "-c", "import dexo_land.settings.production"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr
