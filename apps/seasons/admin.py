import uuid
from typing import cast

from django import forms
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.seasons.close import preview_close, start_or_resume_close
from apps.seasons.models import (
    ClanSeasonFinalStanding,
    PlayerSeason,
    PlayerSeasonFinalStanding,
    Season,
    SeasonCloseRun,
    SeasonSettlementControlEvent,
)
from apps.seasons.services import activate_season, set_settlement_start_epoch


class SettlementControlForm(forms.Form):
    new_epoch = forms.IntegerField(min_value=1)
    reason = forms.CharField(max_length=1000, widget=forms.Textarea)
    ticket_reference = forms.CharField(max_length=128, required=False)
    correlation_id = forms.UUIDField(initial=uuid.uuid4)


class SeasonCloseForm(forms.Form):
    reason = forms.CharField(max_length=1000, widget=forms.Textarea)
    ticket_reference = forms.CharField(max_length=128, required=False)
    correlation_id = forms.UUIDField(initial=uuid.uuid4)


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "code",
        "title",
        "status",
        "starts_at",
        "ends_at",
        "baseline_config",
        "settlement_operation",
    )
    actions = ("activate_selected",)

    @admin.action(description="Activate selected draft season")
    def activate_selected(self, request: HttpRequest, queryset):  # type: ignore[no-untyped-def]
        for season in queryset:
            activate_season(season)

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<int:object_id>/settlement-control/",
                self.admin_site.admin_view(self.settlement_control_view),
                name="season_settlement_control",
            ),
            path(
                "<int:object_id>/close/",
                self.admin_site.admin_view(self.close_view),
                name="season_close",
            ),
        ] + super().get_urls()

    @admin.display(description="Settlement control")
    def settlement_operation(self, obj: Season) -> str:
        return format_html(
            '<a href="{}">Settlement control</a> | <a href="{}">Preview / close</a>',
            reverse("admin:season_settlement_control", args=(obj.pk,)),
            reverse("admin:season_close", args=(obj.pk,)),
        )

    def close_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if not request.user.has_perm("seasons.close_season"):
            raise PermissionDenied
        season = Season.objects.get(pk=object_id)
        report = preview_close(season)
        form = SeasonCloseForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            start_or_resume_close(
                season,
                actor=cast(User, request.user),
                reason=form.cleaned_data["reason"],
                ticket_reference=form.cleaned_data["ticket_reference"],
                correlation_id=form.cleaned_data["correlation_id"],
            )
            return redirect("admin:seasons_season_changelist")
        return TemplateResponse(
            request,
            "admin/seasons/close.html",
            {"form": form, "season": season, "report": report.as_dict()},
        )

    def settlement_control_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if not request.user.has_perm("seasons.change_settlement_control"):
            raise PermissionDenied
        season = Season.objects.get(pk=object_id)
        form = SettlementControlForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            set_settlement_start_epoch(
                season,
                form.cleaned_data["new_epoch"],
                actor=request.user,
                reason=form.cleaned_data["reason"],
                ticket_reference=form.cleaned_data["ticket_reference"],
                correlation_id=form.cleaned_data["correlation_id"],
            )
            return redirect("admin:seasons_season_changelist")
        return TemplateResponse(
            request,
            "admin/seasons/settlement_control.html",
            {"form": form, "season": season},
        )

    def get_readonly_fields(
        self, request: HttpRequest, obj: Season | None = None
    ) -> tuple[str, ...]:
        return (
            (
                "status",
                "settlement_start_epoch",
                "settlement_started_at",
                "created_at",
                "updated_at",
            )
            if obj
            else ("status", "created_at", "updated_at")
        )


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False


@admin.register(SeasonSettlementControlEvent)
class SeasonSettlementControlEventAdmin(ReadOnlyAdmin):
    list_display = (
        "season",
        "previous_start_epoch",
        "new_start_epoch",
        "actor",
        "service_actor",
        "changed_at",
    )


@admin.register(PlayerSeason)
class PlayerSeasonAdmin(ReadOnlyAdmin):
    list_display = ("player", "season", "season_points", "updated_at")


@admin.register(SeasonCloseRun)
class SeasonCloseRunAdmin(ReadOnlyAdmin):
    list_display = ("season", "stage", "correlation_id", "started_at", "completed_at", "failed_at")


@admin.register(PlayerSeasonFinalStanding)
class PlayerSeasonFinalStandingAdmin(ReadOnlyAdmin):
    list_display = ("season", "final_rank", "player", "final_season_points", "finalized_at")


@admin.register(ClanSeasonFinalStanding)
class ClanSeasonFinalStandingAdmin(ReadOnlyAdmin):
    list_display = ("season", "final_rank", "clan", "final_competitive_score", "finalized_at")
