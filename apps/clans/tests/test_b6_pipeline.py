# mypy: disable-error-code="no-untyped-def,no-untyped-call"
from datetime import timedelta
from math import ceil

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from apps.clans.economy import canonical_clan_projection, hq_rule, purchase_item
from apps.clans.missions import process_mission_signals
from apps.clans.models import (
    ClanActivity,
    ClanActivityWindow,
    ClanMembership,
    ClanMembershipEvent,
    ClanMissionDefinition,
    ClanMissionProgress,
    ClanMissionSignal,
    ClanRole,
)
from apps.clans.tests.test_b6_admin import control
from apps.clans.windowing import finalize_due_windows
from apps.core.activity import window_start_for
from apps.rewards.models import RewardLedger
from apps.telegram.services import process_inbound_update
from apps.telegram.tests.test_processing import inbound

pytestmark = pytest.mark.django_db(transaction=True)


def test_ingress_to_windows_missions_and_canonical_projections(client, clan_world):
    now, config, season, resolved, player, clan = clan_world
    policy = resolved.clan
    cs = clan()
    group = cs.clan.telegram_group
    group.is_monitored = group.bot_is_admin = group.bot_can_invite_users = True
    group.save()
    captain = cs.memberships.get()
    soldier = ClanMembership.objects.create(
        player=player(ClanRole.SOLDIER),
        season=season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now,
        config_version=config,
    )
    for member in [captain, soldier]:
        ClanMembershipEvent.objects.create(membership=member, kind="ACTIVATED", occurred_at=now)
    seconds = policy.activity.window_seconds
    horizon = max(
        policy.anti_fraud.exact_duplicate_window_seconds,
        policy.anti_fraud.near_duplicate_window_seconds,
    )
    assert seconds == config.payload["clan"]["activity"]["settlement_window_seconds"] == 900
    start = window_start_for(now, seconds) + timedelta(seconds=seconds)
    next_start = start + timedelta(seconds=seconds)
    n = ceil(10 * policy.activity.chars_per_gross_point / policy.activity.max_rewardable_chars)
    client.force_login(User.objects.create_superuser("b6-pipeline-operator"))
    for kind, requirement, target, band in [
        ("GROUP", "VALID_CLAN_ACTIVITY", n * 2, "GROUP"),
        ("INDIVIDUAL", "CLAN_CONTRIBUTION", 1, "SMALL"),
    ]:
        definition = ClanMissionDefinition.objects.create(
            key=f"pipeline-{kind.lower()}",
            version=1,
            title=kind,
            kind=kind,
            requirement=requirement,
            target_units=target,
            reward_band=band,
            config_version=config,
            starts_at=next_start,
            ends_at=next_start + timedelta(hours=2),
        )
        assert control(client, "clans/clanmissiondefinition", definition).status_code == 302
    sequence = 0

    def ingest(member, at, text):
        nonlocal sequence
        sequence += 1
        update = inbound(
            10000 + sequence,
            {
                "message": {
                    "message_id": sequence,
                    "date": int(at.timestamp()),
                    "chat": {
                        "id": group.telegram_chat_id,
                        "type": "supergroup",
                        "title": group.title,
                    },
                    "from": {"id": member.player.telegram_user_id, "first_name": "Synthetic"},
                    "text": text,
                }
            },
        )
        process_inbound_update(update.pk)
        process_inbound_update(update.pk)
        return update

    ingest(captain, start + timedelta(seconds=1), "z" * policy.activity.min_message_chars)
    due = start + timedelta(seconds=seconds + horizon)
    assert finalize_due_windows(now=due - timedelta(microseconds=1)) == 0
    assert finalize_due_windows(now=due) == 1
    zero = ClanActivityWindow.objects.get()
    assert zero.gross_amount == 0 and zero.finalized_at and zero.settlement_id is None
    assert not RewardLedger.objects.exists() and not cs.treasury_entries.exists()
    process_mission_signals()
    assert not ClanMissionProgress.objects.exists()
    for member in [captain, soldier]:
        for index in range(n):
            at = next_start + timedelta(seconds=1 + index * 61)
            ingest(
                member,
                at,
                chr(65 + index + (10 if member == soldier else 0))
                * policy.activity.max_rewardable_chars,
            )
    assert ClanActivity.objects.count() == 1 + n * 2
    final_at = next_start + timedelta(seconds=seconds + horizon)
    assert finalize_due_windows(now=final_at) == 2
    activity_settlements = list(cs.economic_settlements.filter(source_type="CLAN_ACTIVITY"))
    assert {s.membership_id for s in activity_settlements} == {captain.pk, soldier.pk}
    for settlement in activity_settlements:
        gross = min(
            n * policy.activity.max_rewardable_chars // policy.activity.chars_per_gross_point,
            policy.activity.max_gross_points_per_window,
        )
        assert settlement.gross_amount == gross
        assert settlement.treasury_amount == gross * policy.treasury_split_bps // 10000
        assert settlement.player_amount + settlement.treasury_amount == gross
        assert settlement.contribution_entry.gross_basis == gross
    signal_count = ClanMissionSignal.objects.count()
    assert process_mission_signals() > 0
    assert process_mission_signals() == 0
    assert ClanMissionSignal.objects.count() == signal_count  # Mission rewards do not recurse.
    assert ClanMissionProgress.objects.filter(completed_at__isnull=False).count() == 3
    assert sum(p.participants.count() for p in ClanMissionProgress.objects.all()) == 4
    assert cs.economic_settlements.count() == 6
    ledger_count = RewardLedger.objects.count()
    assert finalize_due_windows(now=final_at + timedelta(days=1)) == 0
    assert process_mission_signals() == 0 and RewardLedger.objects.count() == ledger_count
    cs.refresh_from_db()
    expected = canonical_clan_projection(cs)
    for name, value in expected.items():
        assert getattr(cs, name) == value
    assert cs.hq_level == hq_rule(policy, cs.hq_xp).level
    assert cs.competitive_score > 0 and cs.contribution_total > 0 and cs.treasury_balance > 0
    with pytest.raises(ValidationError, match="[Ii]nsufficient"):
        purchase_item(
            clan_season=cs,
            actor=cs.captain,
            item_id="rally-beacon",
            idempotency_key="b6-insufficient",
            resolved_config=resolved,
        )
