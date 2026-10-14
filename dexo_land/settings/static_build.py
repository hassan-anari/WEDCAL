"""Secret-free, DB-free static build settings using exactly runtime manifest storage."""

import os
from pathlib import Path

from dexo_land.settings.base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "static-build-only-not-a-runtime-secret"
DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
STATIC_ROOT = Path(os.getenv("STATIC_BUILD_ROOT", str(STATIC_ROOT)))  # noqa: F405
