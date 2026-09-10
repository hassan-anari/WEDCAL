from django.apps import AppConfig


class BaseTapConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.base_tap"
