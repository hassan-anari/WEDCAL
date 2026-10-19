"""Shared controlled Admin workflow; preview and activation never settle rewards."""

import hashlib
import json
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from apps.game_config.models import GameConfigVersion
from apps.game_config.services import resolve_effective_game_config, resolve_game_config_version
from apps.notifications.models import OperatorAuditEvent


def validate_definition(row: Any, *, activating: bool = False) -> dict[str, Any]:
    from apps.clans.missions import SUPPORTED_REQUIREMENTS
    from apps.clans.models import ClanMissionDefinition
    from apps.missions.services import _require_policy_binding, require_approved_destination

    row.full_clean()
    if isinstance(row, ClanMissionDefinition):
        if row.requirement not in SUPPORTED_REQUIREMENTS:
            raise ValidationError({"requirement": "Choose a supported server-owned requirement."})
        if row.target_units < 1:
            raise ValidationError({"target_units": "Target must be positive."})
        if row.ends_at <= row.starts_at:
            raise ValidationError({"ends_at": "End must be after start."})
        if not row.title.strip():
            raise ValidationError({"title": "A display title is required."})
        config = row.config_version
        if config.status != GameConfigVersion.Status.PUBLISHED:
            raise ValidationError({"config_version": "Select a published GameConfig."})
        resolved = resolve_game_config_version(config.pk)
        policy = resolved.clan
        band = (
            next((b for b in policy.mission_bands if b.key == row.reward_band), None)
            if policy
            else None
        )
        if not policy or not policy.enabled or band is None:
            raise ValidationError(
                {"reward_band": "Select a reward band from this published Clan policy."}
            )
        if activating and row.ends_at <= timezone.now():
            raise ValidationError("An expired mission cannot be activated.")
        return {
            "config_version": config.version,
            "config_checksum": config.payload_checksum,
            "requirement": row.requirement,
            "kind": row.kind,
            "target_units": row.target_units,
            "participant_gross_points": band.participant_gross_points,
            "direct_hq_xp": band.direct_hq_xp,
            "treasury_share_bps": policy.treasury_split_bps,
            "settlement": "Automatic server-event settlement; no claim action.",
            "eligibility": "Source time in [activation, deactivation) and [start, end).",
            "activation_revision": row.activation_revision,
            "history_unknown_before": (
                row.history_unknown_before.isoformat() if row.history_unknown_before else None
            ),
        }
    resolved = resolve_effective_game_config(timezone.now())
    _require_policy_binding(row, resolved)
    require_approved_destination(row)
    if not resolved.missions.enabled:
        raise ValidationError("Personal missions are disabled in effective GameConfig.")
    if activating and row.available_until and row.available_until <= timezone.now():
        raise ValidationError("An expired mission cannot be activated.")
    rule = resolved.missions.definition(row.key)
    return {
        "config_version": resolved.version,
        "config_checksum": GameConfigVersion.objects.get(pk=resolved.id).payload_checksum,
        "verification_family": rule.verification,
        "repeat_policy": rule.repeat,
        "reward_points": rule.reward_points,
        "parameters": row.verification_parameters,
        "settlement": "Existing verification and idempotent claim workflow.",
        "availability": "Configured",
    }


def fingerprint(row: Any) -> str:
    values = {f.name: getattr(row, f.attname) for f in row._meta.concrete_fields}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


class OperationForm(forms.Form):
    confirmation = forms.CharField(widget=forms.HiddenInput)
    reason = forms.CharField(max_length=1000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="I reviewed the configuration and economic preview")


class ControlledMissionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = None
    semantic_fields: tuple[str, ...] = ()

    def activation_audit_state(self, row: Any) -> dict[str, Any]:
        return {}

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        fields: tuple[str, ...] = ("enabled", "activated_at", "operation")
        if obj:
            fields += ("key", "version")
            if obj.activated_at or self.has_progress(obj):
                fields += self.semantic_fields
        return tuple(dict.fromkeys(fields))

    def has_progress(self, obj: Any) -> bool:
        return bool(obj.progress.exists())

    def get_urls(self) -> list[Any]:
        name = f"{self.model._meta.app_label}_{self.model._meta.model_name}_control"
        return [
            path("<int:object_id>/control/", self.admin_site.admin_view(self.control), name=name)
        ] + super().get_urls()

    @admin.display(description="Preview and activation")
    def operation(self, obj: Any) -> str:
        if not obj.pk:
            return "Save a disabled draft, then open its preview."
        name = f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_control"
        return format_html(
            '<a href="{}">Preview / activate / deactivate</a>', reverse(name, args=(obj.pk,))
        )

    def get_queryset(self, request: HttpRequest):  # type: ignore[no-untyped-def]
        query = super().get_queryset(request)
        change_name = f"{self.model._meta.app_label}_{self.model._meta.model_name}_change"
        if (
            request.method == "POST"
            and request.resolver_match
            and request.resolver_match.url_name == change_name
        ):
            # Django wraps change-form POST in a transaction. Serialize validation
            # and saving with activation, not just the final write.
            query = query.select_for_update()
        return query

    @transaction.atomic
    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        # A stale edit must never undo a separately authorized activation operation.
        if change:
            current = self.model.objects.select_for_update().get(pk=obj.pk)
            obj.enabled, obj.activated_at = current.enabled, current.activated_at
        else:
            obj.enabled = False
        assert request.user.pk is not None
        super().save_model(request, obj, form, change)
        OperatorAuditEvent.objects.create(
            actor_id=request.user.pk,
            action="MISSION_EDIT" if change else "MISSION_CREATE",
            target_type=obj._meta.label,
            target_id=str(obj.pk),
            reason="Django Admin definition form",
            after_state={
                "key": obj.key,
                "version": obj.version,
                "enabled": obj.enabled,
                "changed_fields": list(form.changed_data),
            },
        )

    def control(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if not self.has_view_permission(request):
            raise PermissionDenied
        assert request.user.pk is not None
        row = get_object_or_404(self.model, pk=object_id)
        label = row._meta.label_lower
        salt = f"mission-control:{label}"
        operation = "deactivate" if row.enabled else "activate"
        expected = {
            "id": row.pk,
            "fingerprint": fingerprint(row),
            "operation": operation,
            "actor": request.user.pk,
        }
        token = signing.dumps(expected, salt=salt)
        form = OperationForm(request.POST or None, initial={"confirmation": token})
        preview: dict[str, Any] = {}
        error = ""
        try:
            preview = validate_definition(row, activating=not row.enabled)
        except (ValidationError, LookupError, ValueError):
            error = "Invalid for activation. Check the draft and published GameConfig."
        if request.method == "POST":
            if not self.has_change_permission(request, row) or not request.user.has_perm(
                f"{row._meta.app_label}.activate_{row._meta.model_name}"
            ):
                raise PermissionDenied
            if form.is_valid():
                try:
                    signed = signing.loads(
                        form.cleaned_data["confirmation"], salt=salt, max_age=600
                    )
                    with transaction.atomic():
                        locked = self.model.objects.select_for_update().get(pk=object_id)
                        if signed != expected or fingerprint(locked) != expected["fingerprint"]:
                            raise ValidationError("The preview changed. Review a fresh preview.")
                        if not locked.enabled:
                            validate_definition(locked, activating=True)
                        locked.enabled = not locked.enabled
                        if locked.enabled and locked.activated_at is None:
                            locked.activated_at = timezone.now()
                        locked.save()
                        # Clan lifecycle intervals/revision are database-owned.
                        locked.refresh_from_db()
                        OperatorAuditEvent.objects.create(
                            actor_id=request.user.pk,
                            action="MISSION_ACTIVATE" if locked.enabled else "MISSION_DEACTIVATE",
                            target_type=label,
                            target_id=str(locked.pk),
                            reason=form.cleaned_data["reason"],
                            after_state={
                                "key": locked.key,
                                "version": locked.version,
                                "enabled": locked.enabled,
                                "preview": preview,
                                **self.activation_audit_state(locked),
                            },
                        )
                    self.message_user(request, "Mission configuration updated.", messages.SUCCESS)
                    return redirect(
                        f"admin:{row._meta.app_label}_{row._meta.model_name}_change", row.pk
                    )
                except (signing.BadSignature, ValidationError, LookupError, ValueError):
                    form.add_error(
                        None, "Invalid or stale preview. Review a fresh preview before confirming."
                    )
        return TemplateResponse(
            request,
            "admin/missions/control.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "original": row,
                "form": form,
                "preview": preview,
                "error": error,
                "operation": operation,
                "title": f"Mission {row.key} v{row.version}",
            },
        )
