from dexo_land.settings.base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "deterministic-test-secret-key"
DB_POOL_ENABLED = False
DATABASES["default"]["OPTIONS"] = {}  # noqa: F405
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
# Admin unit tests render static references without running production collectstatic.
# Production keeps CompressedManifestStaticFilesStorage from base settings.
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
}

# Unit/integration tests explicitly opt into mocked notification transport.
ECONOMIC_ALERTS_ENABLED = False
ECONOMIC_ALERT_DELIVERY_ENABLED = False
