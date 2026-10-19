# ruff: noqa: F811
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.arena import models
from apps.arena.admin import ReadOnlyEconomicAdmin
from apps.arena.automation import run_weekly_lifecycle
from apps.arena.inspection import epoch_report, season_report
from apps.arena.recognition import defer_source

from .test_phase4_configurable import configurable_world, milestone  # noqa: F401

AUDIT_MODELS = [
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
    models.PerformanceSourceFact,
    models.EpochReadinessEvaluation,
    models.GrowthEpochFunding,
    models.GrowthEvaluation,
    models.EconomicAutomationControl,
    models.EconomicAutomationAttempt,
]


@pytest.mark.django_db
def test_all_audit_models_registered_strictly_read_only(configurable_world, rf):  # noqa: F811
    w = configurable_world()
    request = rf.get("/")
    request.user = w.actor
    for model in AUDIT_MODELS:
        inspector = admin.site._registry[model]
        assert inspector.has_view_permission(request)
        assert not inspector.has_add_permission(request)
        assert not inspector.has_change_permission(request)
        assert not inspector.has_delete_permission(request)
        assert not inspector.get_actions(request)
        assert not inspector.inlines
        assert inspector.list_per_page <= 50 and not inspector.show_full_result_count


@pytest.mark.django_db
def test_staff_view_permissions_and_sensitive_evidence_redaction(configurable_world, client):  # noqa: F811
    w = configurable_world()
    p = w.player()
    secret = "UNEXPOSED_SECRET_TOKEN_9d2ab"
    case = defer_source(
        w.epochs[0],
        "SYNTHETIC_PENDING",
        "safe-case-1",
        reason="REVIEW",
        player_id=p.pk,
        evidence={"raw_telegram_content": secret, "secret_token": secret},
    )
    content = models.CreatorContentClass.objects.create(
        epoch=w.epochs[0],
        owner=p,
        day=0,
        fingerprint=secret,
        expires_at=w.epochs[0].ends_at + timedelta(days=1),
    )
    staff = User.objects.create_user(username="staff-without-economic-authority", is_staff=True)
    client.force_login(staff)
    target = reverse("admin:arena_economicdeferral_change", args=(case.pk,))
    assert client.get(target).status_code == 403
    assert client.get(reverse("admin:season_economics", args=(w.season.pk,))).status_code == 403
    staff.user_permissions.add(
        Permission.objects.get(content_type__app_label="arena", codename="view_economicdeferral")
    )
    response = client.get(target)
    assert response.status_code == 200
    assert secret.encode() not in response.content
    assert str(p.telegram_user_id).encode() not in response.content
    client.force_login(w.actor)
    assert (
        secret.encode()
        not in client.get(
            reverse("admin:arena_creatorcontentclass_change", args=(content.pk,))
        ).content
    )
    assert client.post(target, {"reason": "rewritten"}).status_code == 403
    assert (
        client.post(
            reverse("admin:arena_economicdeferral_delete", args=(case.pk,)), {"post": "yes"}
        ).status_code
        == 403
    )
    assert client.post(reverse("admin:arena_economicdeferral_add"), {}).status_code == 403
    case.refresh_from_db()
    assert case.reason == "REVIEW"
    assert client.get(
        reverse("admin:arena_economicdeferral_changelist"), {"evidence__secret_token": secret}
    ).status_code in {302, 400}
    policy_page = client.get(
        reverse("admin:arena_economicpolicyversion_change", args=(w.document.pk,))
    )
    assert policy_page.status_code == 200
    assert b"player_cap_bps" in policy_page.content
    assert b"participation_targets" in policy_page.content
    inspector = admin.site._registry[models.EpochPolicyBinding]
    assert isinstance(inspector, ReadOnlyEconomicAdmin)
    assert w.document.checksum in inspector.policy_identity(w.epochs[0].economic_policy)


@pytest.mark.django_db
def test_season_epoch_growth_reports_match_canonical_ledgers(configurable_world, client):  # noqa: F811
    w = configurable_world(milestones=[milestone("one-qualified", 1, 10001)])
    p = w.player()
    for day in (0, 1):
        w.typing(p, w.epochs[0].starts_at + timedelta(days=day, hours=1))
    w.enable()
    w.clock["at"] = w.epochs[0].ends_at
    run_weekly_lifecycle(w.season)
    w.epochs[0].refresh_from_db()
    report = season_report(w.season)
    epoch = epoch_report(w.epochs[0])
    assert report["authorized_growth"] == 10001
    assert report["funding_placements_total"] == 10001
    assert report["authorized_total"] == 110004
    assert report["distinct_qualified_players"] == 1
    assert report["milestones"][0]["status"] == "FUNDED"
    assert epoch["growth"] == 5001 and all(epoch["reconciliation"].values())
    assert report["final_residual"] is None  # No successful close/residual record fabricated.
    client.force_login(w.actor)
    for view, pk in [
        ("admin:season_economics", w.season.pk),
        ("admin:arena_epoch_economics", w.epochs[0].pk),
    ]:
        response = client.get(reverse(view, args=(pk,)))
        assert response.status_code == 200
        assert client.post(reverse(view, args=(pk,)), {}).status_code == 405
    directory = Path("work/phase4-admin-reports")
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in [("season", report), ("epoch", epoch)]:
        (directory / f"{name}.json").write_text(json.dumps(data, indent=2, default=str) + "\n")
    w.clock["at"] = w.epochs[1].ends_at
    run_weekly_lifecycle(w.season)
    final_report = season_report(w.season)
    latest_carry = dict(w.epochs[1].category_budgets.values_list("category", "carry_out"))
    assert {
        row["category"]: row["carry_out"] for row in final_report["category_carry"]
    } == latest_carry
    assert all(row["epoch__epoch_number"] == 2 for row in final_report["category_carry"])


@pytest.mark.django_db
def test_economic_list_pagination_defers_evidence_and_bounds_query_growth(
    configurable_world, client
):  # noqa: F811
    w = configurable_world()
    p = w.player()
    w.typing(p, w.epochs[0].starts_at + timedelta(hours=1))
    receipt = models.EconomicRecognition.objects.get()
    # Synthetic normalized history for list pagination; no economic amounts are allocated.
    from django.db import transaction

    from apps.seasons.barrier import acquire_economic_write_barrier

    with transaction.atomic():
        acquire_economic_write_barrier(w.season.pk)
        rows = []
        for index in range(200):
            values = {
                field.attname: getattr(receipt, field.attname)
                for field in receipt._meta.fields
                if field.name not in {"id", "created_at"}
            }
            values["source_id"] = f"list-inspection:{index}"
            rows.append(models.EconomicRecognition(**values))
        models.EconomicRecognition.objects.bulk_create(rows)
    client.force_login(w.actor)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("admin:arena_economicrecognition_changelist"))
    assert response.status_code == 200
    assert len(response.context["cl"].result_list) == 50
    assert len(queries) < 30
    inspector = admin.site._registry[models.EconomicRecognition]
    request = response.wsgi_request
    first = inspector.get_queryset(request).first()
    assert first is not None and "inputs" in first.get_deferred_fields()
