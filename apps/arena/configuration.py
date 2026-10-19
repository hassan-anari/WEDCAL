"""One authoritative setup snapshot and an explicit immutable policy reference."""

from django.core.exceptions import ValidationError

from apps.arena.models import EconomicPolicyVersion
from apps.arena.policy import EconomicPolicy, digest, parse_policy
from apps.game_config.models import GameConfigVersion
from apps.game_config.services import parse_economy_sync


def validate_policy_compatibility(
    config: GameConfigVersion, document: EconomicPolicyVersion, policy: EconomicPolicy
) -> None:
    _, setup = parse_economy_sync(config.payload)
    if policy.number("days") != setup.epoch_days:
        raise ValidationError("ECONOMIC_POLICY_EPOCH_DURATION_MISMATCH")
    reference = config.payload["dlx_distribution"].get("economic_policy")
    if reference is not None and reference != {
        "key": document.key,
        "version": document.version,
        "checksum": document.checksum,
    }:
        raise ValidationError("GAME_CONFIG_ECONOMIC_POLICY_MISMATCH")
    if setup.growth_milestones and (
        policy.values["implementation"] != "CONFIGURABLE_V1"
        or policy.values["growth"]["rule"] == "DISABLED"
    ):
        raise ValidationError("GROWTH_POLICY_NOT_APPROVED")
    if document.checksum != digest(document.payload) or not document.approval_reference.strip():
        raise ValidationError("ECONOMIC_POLICY_NOT_APPROVED_OR_CORRUPT")


def selected_economic_policy(config: GameConfigVersion) -> EconomicPolicyVersion:
    reference = config.payload["dlx_distribution"].get("economic_policy")
    if not isinstance(reference, dict):
        raise ValidationError("GAME_CONFIG_ECONOMIC_POLICY_REQUIRED")
    try:
        document = EconomicPolicyVersion.objects.get(
            key=reference["key"], version=reference["version"], checksum=reference["checksum"]
        )
    except EconomicPolicyVersion.DoesNotExist as exc:
        raise ValidationError("GAME_CONFIG_ECONOMIC_POLICY_UNAVAILABLE") from exc
    validate_policy_compatibility(config, document, parse_policy(document.payload))
    return document
