"""Stop a destructive downgrade after new configuration/workflow history has been used."""

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


def protect_history(apps, schema_editor):  # type: ignore[no-untyped-def]
    alias = schema_editor.connection.alias
    for name in (
        "GrowthEpochFunding",
        "GrowthEvaluation",
        "EconomicAutomationControl",
        "EconomicAutomationAttempt",
        "EconomicPolicyProposal",
        "EconomicPolicyApproval",
        "EconomicAlert",
        "EconomicAlertEvent",
        "EconomicAlertDelivery",
    ):
        if apps.get_model("arena", name).objects.using(alias).exists():
            raise IrreversibleError(
                "Phase 4 history exists; pause and use a forward compatibility fix"
            )
    if (
        apps.get_model("game_config", "GameConfigVersion")
        .objects.using(alias)
        .filter(schema_version__gte=4, status="PUBLISHED")
        .exists()
    ):
        raise IrreversibleError("Published schema-4 configuration requires forward compatibility")


class Migration(migrations.Migration):
    dependencies = [("arena", "0029_policy_alert_audit_guards")]
    operations = [migrations.RunPython(migrations.RunPython.noop, protect_history)]
