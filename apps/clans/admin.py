from typing import Any

from django import forms
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.clans.models import (
    Clan,
    ClanInvite,
    ClanJoinRequest,
    ClanMembership,
    ClanMembershipEvent,
    ClanSeason,
    PlayerSeasonClanState,
)
from apps.clans.services import resolve_invite_credential_risk


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class InviteRiskForm(forms.Form):
    outcome = forms.ChoiceField(
        choices=[
            ("CONFIRMED_REVOKED", "Confirmed safely revoked"),
            ("STILL_UNSAFE", "Confirmed still unsafe"),
            ("RETRY_REVOCATION", "Retry known credential revocation"),
        ]
    )
    reason = forms.CharField(max_length=1000, widget=forms.Textarea)
    ticket_reference = forms.CharField(max_length=128, required=False)


@admin.register(ClanInvite)
class ClanInviteAdmin(ReadOnlyAdmin):
    exclude = ("invite_link",)
    list_display = (
        "id",
        "purpose",
        "state",
        "unknown_credential_risk",
        "attempt_count",
        "revoke_attempts",
        "risk_operation",
        "updated_at",
    )

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<int:object_id>/resolve-risk/",
                self.admin_site.admin_view(self.resolve_risk_view),
                name="clan_invite_resolve_risk",
            )
        ] + super().get_urls()

    @admin.display(description="Credential risk")
    def risk_operation(self, obj: ClanInvite) -> str:
        if obj.state != ClanInvite.State.MANUAL_REVIEW or not obj.unknown_credential_risk:
            return "Not awaiting review"
        return format_html(
            '<a href="{}">Resolve safely</a>',
            reverse("admin:clan_invite_resolve_risk", args=(obj.pk,)),
        )

    def resolve_risk_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if not request.user.has_perm("clans.resolve_invite_risk"):
            raise PermissionDenied
        invite = ClanInvite.objects.get(pk=object_id)
        form = InviteRiskForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            assert isinstance(request.user, User)
            resolve_invite_credential_risk(
                invite_id=invite.pk,
                outcome=form.cleaned_data["outcome"],
                actor=request.user,
                reason=form.cleaned_data["reason"],
                ticket_reference=form.cleaned_data["ticket_reference"],
            )
            return redirect("admin:clans_claninvite_changelist")
        return TemplateResponse(
            request, "admin/clans/invite_risk.html", {"form": form, "invite": invite}
        )


for model in (
    Clan,
    ClanJoinRequest,
    ClanMembership,
    ClanMembershipEvent,
    ClanSeason,
    PlayerSeasonClanState,
):
    admin.site.register(model, ReadOnlyAdmin)


from apps.clans.models import ClanMissionSignal  # noqa: E402


@admin.register(ClanMissionSignal)
class ClanMissionSignalAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "requirement",
        "units",
        "processed_at",
        "requires_review",
        "attempts",
        "error_category",
    )
    list_filter = ("requires_review", "error_category")
    exclude = ("source_key",)


from apps.clans.missions import SUPPORTED_REQUIREMENTS  # noqa: E402
from apps.clans.models import (  # noqa: E402
    ClanMissionActivation,
    ClanMissionDefinition,
    ClanMissionParticipant,
    ClanMissionProgress,
    ClanMissionSourceEvent,
)
from apps.game_config.models import GameConfigVersion  # noqa: E402
from apps.missions.admin_workflow import ControlledMissionAdmin, validate_definition  # noqa: E402


class ClanMissionDefinitionForm(forms.ModelForm):  # type: ignore[type-arg]
    requirement = forms.ChoiceField(
        choices=[(r, r.replace("_", " ").title()) for r in sorted(SUPPORTED_REQUIREMENTS)]
    )

    class Meta:
        model = ClanMissionDefinition
        exclude = ("enabled", "activated_at")
        help_texts = {
            "reward_band": "Published band key; preview shows actual amounts.",
            "target_units": "Valid event counts or settled contribution units.",
            "config_version": "Match the ClanSeason pinned published config.",
            "version": "Immutable identity. Use a new version for rule changes.",
        }

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        if "config_version" in self.fields:
            field = self.fields["config_version"]
            assert isinstance(field, forms.ModelChoiceField)
            field.queryset = GameConfigVersion.objects.filter(status="PUBLISHED")

    def clean(self):  # type: ignore[no-untyped-def]
        cleaned = super().clean() or {}
        for field in self._meta.model._meta.concrete_fields:
            if field.name in cleaned:
                setattr(self.instance, field.name, cleaned[field.name])
        if not self.errors:
            validate_definition(self.instance)
        return cleaned


@admin.register(ClanMissionDefinition)
class ClanMissionDefinitionAdmin(ControlledMissionAdmin):
    form = ClanMissionDefinitionForm
    list_display = (
        "key",
        "version",
        "kind",
        "requirement",
        "target_units",
        "reward_band",
        "config_version",
        "enabled",
    )
    list_filter = ("enabled", "kind", "requirement", "config_version")
    search_fields = ("key", "title")
    semantic_fields = (
        "kind",
        "requirement",
        "target_units",
        "reward_band",
        "starts_at",
        "ends_at",
        "config_version",
    )

    def has_progress(self, obj):  # type: ignore[no-untyped-def]
        return ClanMissionProgress.objects.filter(definition=obj).exists()

    def get_readonly_fields(self, request, obj=None):  # type: ignore[no-untyped-def]
        return super().get_readonly_fields(request, obj) + (
            "activation_revision",
            "history_unknown_before",
        )

    def activation_audit_state(self, row):  # type: ignore[no-untyped-def]
        interval = row.active_intervals.order_by("-pk").first()
        return {
            "activation_revision": row.activation_revision,
            "activation_interval_id": interval.pk if interval else None,
            "effective_at": (
                (interval.starts_at if row.enabled else interval.ends_at).isoformat()
                if interval
                else None
            ),
        }


@admin.register(ClanMissionActivation)
class ClanMissionActivationAdmin(ReadOnlyAdmin):
    list_display = ("id", "definition", "starts_at", "ends_at", "origin")
    list_select_related = ("definition",)
    list_filter = ("origin",)
    search_fields = ("definition__key",)


for mission_model in (ClanMissionProgress, ClanMissionParticipant, ClanMissionSourceEvent):
    admin.site.register(mission_model, ReadOnlyAdmin)
