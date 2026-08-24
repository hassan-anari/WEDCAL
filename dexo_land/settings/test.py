from dexo_land.settings.base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "deterministic-test-secret-key"
DB_POOL_ENABLED = False
DATABASES["default"]["OPTIONS"] = {}  # noqa: F405
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
