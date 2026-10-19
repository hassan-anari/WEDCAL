"""Read-only economic inspection. Audit models never expose an alternative mutation path."""

import json
from typing import Any, ClassVar

from django.contrib import admin
from django.core.exceptions import FieldDoesNotExist
from django.db import models as db_models
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.arena import models
from apps.arena.inspection import epoch_report, require_auditor
from apps.arena.policy import CATEGORIES, digest
from apps.seasons.models import Season

# Canonical relation paths, used only as query filters; no source history is materialized.
SCOPES = {
    "rankingprojectionevent": ("season_id", None),
    "rankingprojectionstate": ("season_id", None),
    "economicepoch": ("season_id", "pk"),
    "epochanomaly": ("epoch__season_id", "epoch_id"),
    "epochanomalyresolution": ("anomaly__epoch__season_id", "anomaly__epoch_id"),
    "qualificationdecision": (None, None),
    "performancesourcefact": ("season_id", None),
    "epochperformancesnapshot": ("epoch__season_id", "epoch_id"),
    "epochcategorybudget": ("epoch__season_id", "epoch_id"),
    "growthunlockevent": ("season_id", None),
    "growthepochfunding": ("epoch__season_id", "epoch_id"),
    "allocationbatch": ("epoch__season_id", "epoch_id"),
    "dlxallocation": ("season_id", "epoch_id"),
    "dlxadjustment": ("allocation__season_id", "allocation__epoch_id"),
    "seasondlxresidualevent": ("season_id", None),
    "epochreadinessevaluation": ("epoch__season_id", "epoch_id"),
    "economicpolicyversion": (None, None),
    "epochpolicybinding": ("epoch__season_id", "epoch_id"),
    "economicrecognition": ("epoch__season_id", "epoch_id"),
    "economicdeferral": ("epoch__season_id", "epoch_id"),
    "economicdeferralevidence": ("deferral__epoch__season_id", "deferral__epoch_id"),
    "economicdeferralresolution": ("deferral__epoch__season_id", "deferral__epoch_id"),
    "economicreviewdecision": ("epoch__season_id", "epoch_id"),
    "epochsourcemanifest": ("epoch__season_id", "epoch_id"),
    "categorycorrectionreserve": ("budget__epoch__season_id", "budget__epoch_id"),
    "correctionassessment": ("deferral__epoch__season_id", "deferral__epoch_id"),
    "dlxcorrectionround": ("reserve__budget__epoch__season_id", "reserve__budget__epoch_id"),
    "dlxcorrectionentitlement": ("deferral__epoch__season_id", "deferral__epoch_id"),
    "dlxcorrection": ("entitlement__deferral__epoch__season_id", "entitlement__deferral__epoch_id"),
    "dlxcorrectionattempt": (
        "entitlement__deferral__epoch__season_id",
        "entitlement__deferral__epoch_id",
    ),
    "dlxcorrectionadjustment": (
        "correction__entitlement__deferral__epoch__season_id",
        "correction__entitlement__deferral__epoch_id",
    ),
    "creatorcontentclass": ("epoch__season_id", "epoch_id"),
    "economicautomationcontrol": ("season_id", None),
    "economicautomationattempt": ("epoch__season_id", "epoch_id"),
    "growthevaluation": ("season_id", "epoch_id"),
    "economicpolicyproposal": (None, None),
    "economicpolicyapproval": (None, None),
    "economicalert": ("season_id", "epoch_id"),
    "economicalertevent": ("alert__season_id", "alert__epoch_id"),
    "economicalertfanout": ("event__alert__season_id", "event__alert__epoch_id"),
    "economicalertdelivery": ("event__alert__season_id", "event__alert__epoch_id"),
}

POLICY_NUMBERS = frozenset(
    [
        "scale",
        "days",
        "min_age_hours",
        "meaningful_days",
        "daily_only_periods",
        "community_messages_per_day",
        "tap_weight",
        "tap_saturation",
        "typing_weight",
        "typing_runs_per_day",
        "typing_wpm_ceiling",
        "daily_weight",
        "mission_weight",
        "mission_limit",
        "clan_work_weight",
        "clan_consistency_weight",
        "clan_saturation",
        "clan_meaningful_contribution",
        "creator_h_weight",
        "creator_w_weight",
        "creator_h_saturation",
        "creator_window_speakers",
        "creator_window_characters",
        "creator_windows_per_day",
        "creator_speaker_span_seconds",
        "creator_window_seconds",
        "creator_distinct_messages",
        "referral_saturation",
        "minimum_category_score",
        "player_cap_bps",
        "carry_release_bps",
        "correction_reserve_bps",
    ]
)


class SeasonFilter(admin.SimpleListFilter):
    title = "Season (latest 100)"
    parameter_name = "season_id"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [
            (str(pk), code)
            for pk, code in Season.objects.order_by("-pk").values_list("pk", "code")[:100]
        ]

    def queryset(self, request: HttpRequest, queryset: Any) -> Any:
        if self.value() is None or not str(self.value()).isdigit():
            return queryset
        lookup, _ = SCOPES[queryset.model._meta.model_name]
        if lookup:
            return queryset.filter(**{lookup: int(str(self.value()))})
        if queryset.model is models.QualificationDecision:
            return queryset.filter(
                evidence__epoch_id__in=list(
                    models.EconomicEpoch.objects.filter(
                        season_id=int(str(self.value()))
                    ).values_list("pk", flat=True)
                )
            )
        if queryset.model is models.EconomicPolicyVersion:
            return queryset.filter(
                pk__in=models.EpochPolicyBinding.objects.filter(
                    epoch__season_id=int(str(self.value()))
                ).values("policy_id")
            )
        return queryset


class EpochFilter(admin.SimpleListFilter):
    title = "Epoch (latest 100)"
    parameter_name = "epoch_id"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [
            (str(pk), f"{season} / {number}")
            for pk, season, number in models.EconomicEpoch.objects.order_by("-pk").values_list(
                "pk", "season__code", "epoch_number"
            )[:100]
        ]

    def queryset(self, request: HttpRequest, queryset: Any) -> Any:
        if self.value() is None or not str(self.value()).isdigit():
            return queryset
        _, lookup = SCOPES[queryset.model._meta.model_name]
        if lookup:
            return queryset.filter(**{lookup: int(str(self.value()))})
        if queryset.model is models.QualificationDecision:
            return queryset.filter(evidence__epoch_id=int(str(self.value())))
        if queryset.model is models.EconomicPolicyVersion:
            return queryset.filter(
                pk__in=models.EpochPolicyBinding.objects.filter(
                    epoch_id=int(str(self.value()))
                ).values("policy_id")
            )
        if queryset.model is models.GrowthUnlockEvent:
            return queryset.filter(
                Q(funding__epoch_id=int(str(self.value())))
                | Q(evidence__recognition_epoch_id=int(str(self.value())))
            ).distinct()
        if queryset.model is models.PerformanceSourceFact:
            epoch = models.EconomicEpoch.objects.filter(pk=int(str(self.value()))).first()
            return (
                queryset.filter(
                    season_id=epoch.season_id,
                    source_time__gte=epoch.starts_at,
                    source_time__lt=epoch.ends_at,
                )
                if epoch
                else queryset.none()
            )
        return queryset


class ReadOnlyEconomicAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = None
    inlines: ClassVar[tuple[Any, ...]] = ()
    list_per_page = 50
    list_max_show_all = 50
    show_full_result_count = False
    ordering = ("-pk",)
    search_fields = ("=id",)

    def get_queryset(self, request: HttpRequest) -> Any:
        hidden = [
            field.name
            for field in self.model._meta.fields
            if isinstance(field, (db_models.JSONField, db_models.TextField))
            or field.name
            in {
                "fingerprint",
                "source_id",
                "evidence_reference",
                "approval_reference",
                "submission_reference",
            }
        ]
        return super().get_queryset(request).defer(*hidden)

    def lookup_allowed(self, lookup: str, value: str, request: HttpRequest | None = None) -> bool:
        model: Any = self.model
        for part in lookup.split("__"):
            try:
                field = model._meta.get_field(part)
            except FieldDoesNotExist:
                break
            if isinstance(field, db_models.JSONField):
                return False
            if not field.is_relation or field.related_model is None:
                break
            model = field.related_model
        if any(
            part
            in {
                "payload",
                "evidence",
                "inputs",
                "metadata",
                "raw_inputs",
                "fingerprint",
                "text",
                "source_id",
                "evidence_reference",
                "approval_reference",
                "submission_reference",
                "route_config",
                "reason",
                "note",
                "telegram_user_id",
            }
            for part in lookup.split("__")
        ):
            return False
        return super().lookup_allowed(lookup, value, request)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        hidden = {
            "telegram_user_id",
            "lease_token",
            "fingerprint",
            "source_id",
            "reason",
            "note",
            "text",
            "evidence_reference",
            "approval_reference",
            "submission_reference",
        }
        fields = []
        for field in self.model._meta.fields:
            if field.name in hidden or isinstance(
                field, (db_models.JSONField, db_models.TextField)
            ):
                continue
            if field.is_relation and field.remote_field.model._meta.label_lower == "players.player":
                continue  # Player.__str__ contains a Telegram user ID.
            fields.append(field.name)
        return (*fields, "entity_references", "safe_provenance", "related_history")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return self.get_fields(request, obj)

    @admin.display(description="Entity references (internal IDs)")
    def entity_references(self, obj: Any) -> str:
        return ", ".join(
            f"{field.name}#{getattr(obj, field.attname)}"
            for field in obj._meta.fields
            if field.is_relation and field.remote_field.model._meta.label_lower == "players.player"
        )

    @admin.display(description="Economic policy identity")
    def policy_identity(self, obj: Any) -> str:
        policy = obj.policy
        return f"{policy.key} v{policy.version} / {policy.checksum}"

    @admin.display(description="Redacted provenance / evidence digests")
    def safe_provenance(self, obj: Any) -> str:
        safe: dict[str, Any] = {}
        keys = {
            "rule",
            "outcome",
            "threshold",
            "requested_amount",
            "qualifying_cutoff",
            "evaluated_at",
            "recognition_epoch_id",
            "recognition_epoch_number",
            "cohort_digest",
            "policy_id",
            "policy_checksum",
            "config_checksum",
            "evaluation_id",
            "counted_population",
            "implementation",
        }
        if isinstance(obj, models.EconomicAlertEvent):
            keys |= {
                "delivery_id",
                "source_event_id",
                "error_code",
                "transport_code",
                "attempt",
                "status",
                "action",
                "resulting_status",
                "actor_id",
                "reference_digest",
                "previous_status",
            }
        for field in obj._meta.fields:
            value = getattr(obj, field.name)
            if isinstance(field, db_models.JSONField):
                safe[field.name] = {"digest": digest(value)}
                if isinstance(value, dict):
                    safe[field.name]["summary"] = {
                        key: value[key]
                        for key in keys
                        if key in value and isinstance(value[key], (str, int, bool, type(None)))
                    }
                    if isinstance(
                        obj, (models.EconomicPolicyVersion, models.EconomicPolicyProposal)
                    ):
                        safe[field.name]["parameters"] = {
                            key: value[key] for key in POLICY_NUMBERS if type(value.get(key)) is int
                        }
                        targets = value.get("participation_targets", {})
                        safe[field.name]["participation_targets"] = {
                            key: targets[key] for key in CATEGORIES if type(targets.get(key)) is int
                        }
                        safe[field.name]["growth_rule"] = value.get("growth", {}).get("rule")
        return json.dumps(safe, sort_keys=True)

    @admin.display(description="Related audit history")
    def related_history(self, obj: Any) -> str:
        if isinstance(obj, models.GrowthUnlockEvent):
            return format_html(
                '<a href="{}?event__id__exact={}">Receiving-Epoch funding</a>',
                reverse("admin:arena_growthepochfunding_changelist"),
                obj.pk,
            )
        _, epoch_lookup = SCOPES[obj._meta.model_name]
        if epoch_lookup:
            # Related navigation uses a scalar epoch ID only; it never renders source payloads.
            current: Any = obj
            for part in epoch_lookup.split("__"):
                current = getattr(current, part)
            if current is None:
                return "—"
            return format_html(
                '<a href="{}">Epoch economic overview</a>',
                reverse("admin:arena_epoch_economics", args=(current,)),
            )
        return "—"


class EconomicEpochAdmin(ReadOnlyEconomicAdmin):
    def get_urls(self) -> list[Any]:
        return [
            path(
                "<int:object_id>/economics/",
                self.admin_site.admin_view(self.economics_view),
                name="arena_epoch_economics",
            )
        ] + super().get_urls()

    def economics_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        require_auditor(request.user)
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        report = epoch_report(
            models.EconomicEpoch.objects.select_related("season").get(pk=object_id)
        )
        return TemplateResponse(
            request,
            "admin/arena/economics.html",
            {
                "title": "Epoch economic inspection",
                "report": json.dumps(report, indent=2, default=str),
            },
        )


for model in (
    models.RankingProjectionEvent,
    models.RankingProjectionState,
    models.QualificationDecision,
    models.EconomicEpoch,
    models.EpochAnomaly,
    models.EpochAnomalyResolution,
    models.EpochPerformanceSnapshot,
    models.PerformanceSourceFact,
    models.EpochCategoryBudget,
    models.GrowthUnlockEvent,
    models.GrowthEpochFunding,
    models.AllocationBatch,
    models.DlxAllocation,
    models.DlxAdjustment,
    models.SeasonDlxResidualEvent,
    models.EpochReadinessEvaluation,
    models.EconomicPolicyVersion,
    models.EpochPolicyBinding,
    models.EconomicRecognition,
    models.EconomicDeferral,
    models.EconomicDeferralEvidence,
    models.EconomicDeferralResolution,
    models.EconomicReviewDecision,
    models.EpochSourceManifest,
    models.CategoryCorrectionReserve,
    models.CorrectionAssessment,
    models.DlxCorrectionRound,
    models.DlxCorrectionEntitlement,
    models.DlxCorrection,
    models.DlxCorrectionAttempt,
    models.DlxCorrectionAdjustment,
    models.CreatorContentClass,
    models.EconomicAutomationControl,
    models.EconomicAutomationAttempt,
    models.GrowthEvaluation,
    models.EconomicPolicyProposal,
    models.EconomicPolicyApproval,
    models.EconomicAlert,
    models.EconomicAlertEvent,
    models.EconomicAlertDelivery,
    models.EconomicAlertFanout,
):
    names = {field.name for field in model._meta.fields}
    display = ["id"] + [
        name
        for name in (
            "key",
            "version",
            "code",
            "severity",
            "route_key",
            "channel",
            "last_error_code",
            "telegram_message_id",
            "lease_expires_at",
            "stage",
            "attempts",
            "season",
            "epoch",
            "category",
            "milestone_key",
            "state",
            "status",
            "outcome",
            "decision",
            "amount",
            "unlocked_amount",
            "qualified_player_count",
            "policy",
            "config_version",
            "checksum",
            "evidence_digest",
            "input_digest",
            "created_at",
            "effective_at",
            "confirmed_at",
            "opened_at",
            "resolved_at",
            "started_at",
            "completed_at",
            "next_attempt_at",
            "delivered_at",
        )
        if name in names
    ]
    if "policy" in names:
        display[display.index("policy")] = "policy_identity"
    season_scope, epoch_scope = SCOPES[model._meta.model_name or ""]
    filters: list[Any] = []
    if season_scope or model in {models.QualificationDecision, models.EconomicPolicyVersion}:
        filters.append(SeasonFilter)
    if epoch_scope or model in {
        models.QualificationDecision,
        models.EconomicPolicyVersion,
        models.PerformanceSourceFact,
        models.GrowthUnlockEvent,
    }:
        filters.append(EpochFilter)
    filters += [
        name
        for name in ("category", "state", "status", "outcome", "decision", "code", "severity")
        if name in names
    ]
    related = tuple(
        field.name
        for field in model._meta.fields
        if field.is_relation
        and field.remote_field is not None
        and field.remote_field.model._meta.label_lower != "players.player"
    )
    base = EconomicEpochAdmin if model is models.EconomicEpoch else ReadOnlyEconomicAdmin
    inspector = type(
        f"{model.__name__}Inspector",
        (base,),
        {
            "list_display": tuple(display),
            "list_filter": tuple(filters),
            "list_select_related": related,
            "search_fields": ("=id", "=milestone_key")
            if "milestone_key" in names
            else ("=id", "=key")
            if model in {models.EconomicPolicyVersion, models.EconomicPolicyProposal}
            else ("=id",),
        },
    )
    admin.site.register(model, inspector)
