# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, close_old_connections, connections, transaction

from apps.arena.models import (
    EconomicAutomationControl,
    EconomicPolicyApproval,
    EconomicPolicyProposal,
    EconomicPolicyVersion,
    EpochPolicyBinding,
)
from apps.arena.policy import digest
from apps.arena.policy_registration import approve_policy, propose_policy, validate_document

PAYLOAD = json.loads(Path("config/examples/configurable.economic-policy.v1.json").read_text())


@pytest.fixture
def operators(db):
    author = User.objects.create_user("policy-author", is_staff=True)
    reviewer = User.objects.create_user("policy-reviewer", is_staff=True)
    author.user_permissions.add(Permission.objects.get(codename="propose_economic_policy"))
    reviewer.user_permissions.add(Permission.objects.get(codename="approve_economic_policy"))
    return author, reviewer


def proposed(author):
    return propose_policy(
        key="REGISTERED_TEST",
        version=1,
        payload=PAYLOAD,
        actor=author,
        submission_reference="synthetic-source-review",
        expected_checksum=digest(PAYLOAD),
        dry_run=False,
    )["proposal_id"]


@pytest.mark.django_db
def test_command_two_person_dry_run_and_exact_registration(operators):
    author, reviewer = operators
    output = io.StringIO()
    args = [
        "propose",
        "--actor",
        author.username,
        "--reference",
        "synthetic-source-review",
        "--key",
        "REGISTERED_TEST",
        "--policy-version",
        "1",
        "--file",
        "config/examples/configurable.economic-policy.v1.json",
    ]
    call_command("register_economic_policy", *args, stdout=output)
    assert json.loads(output.getvalue())["checksum"] == digest(PAYLOAD)
    assert not EconomicPolicyProposal.objects.exists()
    with pytest.raises(CommandError, match="CONFIRMED_CHECKSUM"):
        call_command("register_economic_policy", *args, "--apply")
    call_command(
        "register_economic_policy",
        *args,
        "--checksum",
        digest(PAYLOAD),
        "--apply",
        stdout=io.StringIO(),
    )
    proposal = EconomicPolicyProposal.objects.get()
    approval_args = [
        "approve",
        "--actor",
        reviewer.username,
        "--reference",
        "synthetic-owner-approval",
        "--proposal",
        str(proposal.pk),
        "--checksum",
        digest(PAYLOAD),
    ]
    output = io.StringIO()
    call_command("register_economic_policy", *approval_args, stdout=output)
    assert json.loads(output.getvalue())["validated_payload"] == PAYLOAD
    assert (
        not EconomicPolicyVersion.objects.exists() and not EconomicPolicyApproval.objects.exists()
    )
    for _ in range(2):
        call_command("register_economic_policy", *approval_args, "--apply", stdout=io.StringIO())
    document = EconomicPolicyVersion.objects.get()
    audit = EconomicPolicyApproval.objects.get()
    assert document.payload == PAYLOAD and document.checksum == digest(PAYLOAD)
    assert audit.proposal.submitted_by_id == author.pk and audit.approved_by_id == reviewer.pk
    assert (
        audit.policy_id == document.pk and audit.approval_reference == document.approval_reference
    )
    assert (
        not EpochPolicyBinding.objects.exists() and not EconomicAutomationControl.objects.exists()
    )
    for model in (EconomicPolicyProposal, EconomicPolicyApproval, EconomicPolicyVersion):
        with pytest.raises(DatabaseError), transaction.atomic():
            model.objects.all().delete()
    directory = Path("work/phase4-policy-reports")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "registration.json").write_text(
        json.dumps(
            {
                "synthetic": True,
                "checksum": document.checksum,
                "key": document.key,
                "version": document.version,
                "proposal_id": proposal.pk,
                "policy_id": document.pk,
                "submitted_by_id": author.pk,
                "approved_by_id": reviewer.pk,
                "independent_actors": author.pk != reviewer.pk,
                "dry_runs_created_no_rows": True,
                "retry_created_one_policy": EconomicPolicyVersion.objects.count() == 1,
                "bindings_created": EpochPolicyBinding.objects.count(),
                "automation_controls_created": EconomicAutomationControl.objects.count(),
            },
            indent=2,
        )
        + "\n"
    )


@pytest.mark.django_db
def test_permissions_independent_approval_checksum_and_conflicts(operators):
    author, reviewer = operators
    stranger = User.objects.create_user("not-authorized", is_staff=True)
    with pytest.raises(PermissionDenied):
        proposed(stranger)
    proposal_id = proposed(author)
    assert proposed(author) == proposal_id
    author.is_superuser = True
    author.save(update_fields=["is_superuser"])
    with pytest.raises(PermissionDenied, match="INDEPENDENT"):
        approve_policy(
            proposal_id,
            actor=author,
            approval_reference="no-self-approval",
            expected_checksum=digest(PAYLOAD),
            dry_run=False,
        )
    with pytest.raises(ValidationError, match="CHECKSUM"):
        approve_policy(
            proposal_id,
            actor=reviewer,
            approval_reference="review",
            expected_checksum="0" * 64,
            dry_run=False,
        )
    assert not EconomicPolicyVersion.objects.exists()
    other = copy.deepcopy(PAYLOAD)
    other["tap_saturation"] += 1
    with pytest.raises(ValidationError, match="CONFLICT"):
        propose_policy(
            key="REGISTERED_TEST",
            version=1,
            payload=other,
            actor=author,
            submission_reference="synthetic-source-review",
            expected_checksum=digest(other),
            dry_run=False,
        )
    reviewer.is_active = False
    with pytest.raises(PermissionDenied):
        approve_policy(
            proposal_id,
            actor=reviewer,
            approval_reference="review",
            expected_checksum=digest(PAYLOAD),
            dry_run=False,
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(unknown_secret="not-a-policy-field"),
        lambda p: p.update(days=True),
        lambda p: p.update(player_cap_bps=10001),
        lambda p: p["participation_targets"].update(CORE_GAMEPLAY=False),
        lambda p: p.update(growth={"rule": "UNAPPROVED"}),
        lambda p: p.update(independent_mission_keys=[{}]),
    ],
)
def test_strict_schema_keeps_existing_economic_validation(change):
    payload = copy.deepcopy(PAYLOAD)
    change(payload)
    with pytest.raises(ValidationError):
        validate_document(payload)


@pytest.mark.django_db
def test_duplicate_json_keys_rejected_before_mutation(operators, tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"days":7,"days":1}')
    with pytest.raises(CommandError, match="Duplicate JSON"):
        call_command(
            "register_economic_policy",
            "propose",
            "--actor",
            operators[0].username,
            "--reference",
            "synthetic",
            "--key",
            "DUPLICATE",
            "--policy-version",
            "1",
            "--file",
            str(path),
            "--apply",
        )
    assert not EconomicPolicyProposal.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("missed_full_period_resets_streak", False),
        ("catch_up_claims_enabled", True),
        ("cycle_restarts_after_day_7", False),
        ("streak_counter_continues_after_day_7", False),
    ],
)
def test_unsupported_daily_lifecycle_cannot_publish_as_tuning(phase4_config_factory, field, value):
    with pytest.raises(ValidationError, match="Unsupported Daily lifecycle"):
        phase4_config_factory(mutate=lambda p: p["daily"].update({field: value}))


@pytest.mark.django_db(transaction=True)
def test_concurrent_approval_is_one_immutable_registration(operators):
    author, reviewer = operators
    proposal_id = proposed(author)
    barrier = Barrier(2)

    def worker(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return approve_policy(
                proposal_id,
                actor=reviewer,
                approval_reference="same-reviewed-reference",
                expected_checksum=digest(PAYLOAD),
                dry_run=False,
            )["policy_id"]
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(worker, range(2)))
    assert result[0] == result[1]
    assert EconomicPolicyVersion.objects.count() == EconomicPolicyApproval.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_populated_policy_history_refuses_destructive_downgrade(operators):
    from django.db import connection
    from django.db.migrations.exceptions import IrreversibleError
    from django.db.migrations.executor import MigrationExecutor
    from django.db.migrations.recorder import MigrationRecorder

    proposal_id = proposed(operators[0])
    with pytest.raises(IrreversibleError, match="history exists"):
        MigrationExecutor(connection).migrate([("arena", "0029_policy_alert_audit_guards")])
    assert EconomicPolicyProposal.objects.get(pk=proposal_id).checksum == digest(PAYLOAD)
    assert MigrationRecorder.Migration.objects.filter(
        app="arena", name="0030_protect_feature_history_on_reverse"
    ).exists()


def test_published_registration_schema_and_examples_agree():
    from apps.arena.policy_registration import POLICY_NUMBERS

    schema = json.loads(Path("config/schemas/economic-policy.schema.json").read_text())
    assert set(schema["oneOf"][0]["required"]) == set(PAYLOAD)
    assert set(schema["oneOf"][0]["properties"]) >= POLICY_NUMBERS
    for path in Path("config/examples").glob("*economic-policy*.json"):
        assert validate_document(json.loads(path.read_text()))
