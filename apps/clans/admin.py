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
    list_display = ("id", "requirement", "units", "processed_at", "attempts", "error_category")
    exclude = ("source_key",)
