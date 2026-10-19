# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from datetime import timedelta

import pytest

from apps.clans.missions import record_mission_progress
from apps.clans.models import (
    ClanHQXPEntry,
    ClanMembership,
    ClanMembershipEvent,
    ClanMissionDefinition,
)

BANDS = {"SMALL": (211, 37), "MEDIUM": (307, 43), "LARGE": (401, 59), "GROUP": (503, 61)}


@pytest.fixture
def phase4_config_factory(phase4_config_factory):
    """Change only synthetic input, using the existing validated publication fixture."""

    def configured(*args, **kwargs):
        def mutate(payload):
            for key, (gross, hq) in BANDS.items():
                payload["clan"]["missions"]["reward_bands"][key] = {
                    "participant_gross_points": gross,
                    "direct_hq_xp": hq,
                }

        return phase4_config_factory(*args, mutate=mutate, **kwargs)

    return configured


@pytest.mark.django_db
@pytest.mark.parametrize("band", list(BANDS))
def test_new_active_season_missions_use_configured_band_amounts_exactly_once(clan_world, band):
    now, config, season, resolved, player, clan = clan_world
    cs = clan()
    captain = cs.memberships.get()
    members = [captain]
    if band == "GROUP":
        members.append(
            ClanMembership.objects.create(
                player=player("SOLDIER"),
                season=season,
                clan_season=cs,
                role="SOLDIER",
                started_at=now,
                config_version=config,
            )
        )
    for member in members:
        ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    definition = ClanMissionDefinition.objects.create(
        key=f"new-midseason-{band.lower()}",
        version=1,
        kind="GROUP" if band == "GROUP" else "INDIVIDUAL",
        requirement="TEST",
        target_units=len(members),
        reward_band=band,
        starts_at=now,
        ends_at=now + timedelta(days=1),
        enabled=True,
        config_version=config,
    )
    at = definition.active_intervals.get().starts_at
    for index, member in enumerate(members):
        for _ in range(2):
            progress = record_mission_progress(
                definition=definition,
                membership=member,
                source_id=f"configured-band-{index}",
                units=1,
                occurred_at=at,
                resolved_config=resolved,
            )
    assert progress.completed_at
    participants = list(progress.participants.select_related("reward_settlement"))
    assert len(participants) == len(members)
    for participant in participants:
        assert participant.reward_settlement is not None
        assert participant.reward_settlement.gross_amount == BANDS[band][0]
    hq = ClanHQXPEntry.objects.get(idempotency_key=f"clan-mission-direct-hq:{progress.pk}")
    assert hq.amount == BANDS[band][1] and hq.config_version_id == config.pk
