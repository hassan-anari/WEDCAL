from django import forms
from django.contrib import admin

from apps.missions.models import MissionDefinition, PlayerMission


class MissionDefinitionForm(forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = MissionDefinition
        fields = "__all__"
        help_texts = {
            "verification_parameters": (
                'JSON object. Timed external requires "destination_url": "https://example.com/". '
                'Telegram verification requires numeric "telegram_chat_id"; it may also have a '
                "destination_url for the join button. Reward and verification policy come from "
                "the matching published, effective GameConfig rule."
            )
        }


@admin.register(MissionDefinition)
class MissionDefinitionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    form = MissionDefinitionForm
    list_display = ("key", "version", "verification_family", "repeat_policy", "enabled")
    list_filter = ("enabled", "verification_family", "repeat_policy")

    def get_readonly_fields(self, request, obj=None):  # type: ignore[no-untyped-def]
        if obj:
            return ("key", "version", "verification_family", "repeat_policy", "created_at")
        return ("created_at",)


@admin.register(PlayerMission)
class PlayerMissionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("player", "definition", "eligibility_period", "status", "reward_amount")
    list_filter = ("status", "definition__verification_family", "season", "config_version")
    readonly_fields = tuple(field.name for field in PlayerMission._meta.fields)

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False
