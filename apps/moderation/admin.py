import uuid
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

from apps.moderation.models import (
    ModerationAction,
    ModerationCase,
    ModerationEvidence,
    ModerationEvidenceAccess,
)
from apps.moderation.services import add_manual_note, apply_action, view_sensitive_evidence


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ModerationActionForm(forms.Form):
    action_type = forms.ChoiceField(
        choices=[
            (value, label)
            for value, label in ModerationAction.Type.choices
            if value != ModerationAction.Type.FLAG
        ]
    )
    reason = forms.CharField(max_length=1000, widget=forms.Textarea)
    suspension_expires_at = forms.DateTimeField(required=False)
    idempotency_key = forms.CharField(max_length=128, initial=uuid.uuid4)
    correlation_id = forms.UUIDField(initial=uuid.uuid4)


class EvidenceAccessForm(forms.Form):
    reason = forms.CharField(max_length=1000, widget=forms.Textarea)


class ManualNoteForm(forms.Form):
    note = forms.CharField(max_length=4000, widget=forms.Textarea)
    reason = forms.CharField(max_length=1000, widget=forms.Textarea)


@admin.register(ModerationCase)
class ModerationCaseAdmin(ReadOnlyAdmin):
    list_display = ("public_id", "target", "category", "status", "created_at", "operations")
    list_filter = ("status", "category")

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<int:object_id>/apply-action/",
                self.admin_site.admin_view(self.apply_action_view),
                name="moderation_case_apply_action",
            ),
            path(
                "<int:object_id>/add-note/",
                self.admin_site.admin_view(self.add_note_view),
                name="moderation_case_add_note",
            ),
        ] + super().get_urls()

    @admin.display(description="Controlled operations")
    def operations(self, obj: ModerationCase) -> str:
        return format_html(
            '<a href="{}">Apply action</a> · <a href="{}">Add note</a>',
            reverse("admin:moderation_case_apply_action", args=(obj.pk,)),
            reverse("admin:moderation_case_add_note", args=(obj.pk,)),
        )

    def apply_action_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if not request.user.has_perm("moderation.apply_moderation_actions"):
            raise PermissionDenied
        case = ModerationCase.objects.get(pk=object_id)
        form = ModerationActionForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            assert isinstance(request.user, User)
            apply_action(
                case_id=case.pk,
                action_type=form.cleaned_data["action_type"],
                reason=form.cleaned_data["reason"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                staff_actor=request.user,
                suspension_expires_at=form.cleaned_data["suspension_expires_at"],
                correlation_id=form.cleaned_data["correlation_id"],
            )
            return redirect("admin:moderation_moderationcase_changelist")
        return TemplateResponse(
            request,
            "admin/moderation/operation_form.html",
            {"form": form, "case": case, "title": "Apply moderation action"},
        )

    def add_note_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if not request.user.has_perm("moderation.apply_moderation_actions"):
            raise PermissionDenied
        case = ModerationCase.objects.get(pk=object_id)
        form = ManualNoteForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            assert isinstance(request.user, User)
            add_manual_note(
                case_id=case.pk,
                note=form.cleaned_data["note"],
                reason=form.cleaned_data["reason"],
                staff_actor=request.user,
            )
            return redirect("admin:moderation_moderationcase_changelist")
        return TemplateResponse(
            request,
            "admin/moderation/operation_form.html",
            {"form": form, "case": case, "title": "Add moderation note"},
        )


@admin.register(ModerationEvidence)
class ModerationEvidenceAdmin(ReadOnlyAdmin):
    exclude = ("manual_note",)
    list_display = ("id", "case", "kind", "created_at", "secure_view")

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<int:object_id>/secure-view/",
                self.admin_site.admin_view(self.secure_evidence_view),
                name="moderation_evidence_secure_view",
            )
        ] + super().get_urls()

    @admin.display(description="Sensitive evidence")
    def secure_view(self, obj: ModerationEvidence) -> str:
        return format_html(
            '<a href="{}">Audited view</a>',
            reverse("admin:moderation_evidence_secure_view", args=(obj.pk,)),
        )

    def secure_evidence_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if not request.user.has_perm("moderation.view_sensitive_evidence"):
            raise PermissionDenied
        evidence = ModerationEvidence.objects.get(pk=object_id)
        form = EvidenceAccessForm(request.POST or None)
        result = None
        if request.method == "POST" and form.is_valid():
            assert isinstance(request.user, User)
            result = view_sensitive_evidence(
                evidence_id=evidence.pk,
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        return TemplateResponse(
            request,
            "admin/moderation/evidence_view.html",
            {"form": form, "evidence": evidence, "result": result},
        )

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return request.user.has_perm("moderation.view_sensitive_evidence")


admin.site.register(ModerationAction, ReadOnlyAdmin)
admin.site.register(ModerationEvidenceAccess, ReadOnlyAdmin)
