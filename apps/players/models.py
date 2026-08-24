from django.db import models


class Player(models.Model):
    class FirstSeenSource(models.TextChoices):
        BOT_START = "BOT_START", "Bot start"
        MINI_APP = "MINI_APP", "Mini App"
        GROUP_ACTIVITY = "GROUP_ACTIVITY", "Group activity"

    telegram_user_id = models.BigIntegerField(unique=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True)
    language_code = models.CharField(max_length=35, blank=True)
    photo_url = models.URLField(max_length=2048, blank=True)
    profile_observed_at = models.DateTimeField(null=True, blank=True)
    profile_last_telegram_update_id = models.BigIntegerField(null=True, blank=True)
    first_seen_at = models.DateTimeField()
    first_seen_source = models.CharField(max_length=20, choices=FirstSeenSource.choices)
    first_private_contact_at = models.DateTimeField(null=True, blank=True)
    first_mini_app_at = models.DateTimeField(null=True, blank=True)
    first_start_parameter = models.CharField(max_length=255, blank=True)
    last_seen_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Telegram user {self.telegram_user_id}"
