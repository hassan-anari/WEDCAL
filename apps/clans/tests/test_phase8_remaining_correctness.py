from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.clans.economy import settle_clan_reward
from apps.clans.missions import record_mission_progress
from apps.clans.models import (
    ClanActivity,
    ClanCaptainBonusDaily,
    ClanMembership,
    ClanMembershipEvent,
    ClanMissionDefinition,
    ClanMissionParticipant,
    ClanRole,
)
from apps.clans.windowing import finalize_due_windows
from apps.core.activity import window_start_for
from apps.game_config.services import resolve_game_config_version
from apps.moderation.models import ModerationAction, ModerationCase
from apps.network.models import CreatorGroupRegistration, CreatorRegistrationStateEvent
from apps.network.services import registration_reward_eligible_at
from apps.players.models import Player
from apps.players.services import reward_gameplay_allowed_at
from apps.rewards.models import RewardLedger


def activated(member: ClanMembership, at=None) -> None:  # type: ignore[no-untyped-def]
    ClanMembershipEvent.objects.create(
        membership=member, kind="ACTIVATED", occurred_at=at or member.started_at
    )


@pytest.mark.django_db
def test_captain_bonus_config_cutover_keeps_one_daily_budget(
    clan_world: Any, phase4_config_factory: Any
) -> None:
    now, config_a, _, resolved_a, player, clan = clan_world
    cs = clan()
    captain = cs.memberships.get()
    activated(captain)
    soldier_player = player(ClanRole.SOLDIER)
    soldier = ClanMembership.objects.create(
        player=soldier_player,
        season=cs.season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now - timedelta(minutes=1),
        config_version=config_a,
    )
    activated(soldier)
    settle_clan_reward(
        membership=soldier,
        source_type="CLAN_ACTIVITY",
        source_id="cutover-a",
        gross_amount=100,
        idempotency_key="cutover-a",
        occurred_at=now,
        resolved_config=resolved_a,
        metadata={"captain_bonus_gross_amount": 100},
    )
    config_b = phase4_config_factory(
        effective_at=now + timedelta(seconds=1),
        version="captain-cutover",
        mutate=lambda p: p["clan"]["captain_bonus"].update(share_bps=1000, max_points_per_day=5),
    )
    resolved_b = resolve_game_config_version(config_b.pk)
    settle_clan_reward(
        membership=soldier,
        source_type="CLAN_ACTIVITY",
        source_id="cutover-b",
        gross_amount=100,
        idempotency_key="cutover-b",
        occurred_at=now + timedelta(seconds=1),
        resolved_config=resolved_b,
        metadata={"captain_bonus_gross_amount": 100},
    )
    daily = ClanCaptainBonusDaily.objects.get(clan_season=cs)
    assert daily.awarded_amount == 5  # A awards 2; B may use only 3 of its configured cap.
    assert daily.sources.count() == 2 and daily.config_accumulators.count() == 2
    assert list(daily.sources.order_by("occurred_at").values_list("awarded_amount", flat=True)) == [
        2,
        3,
    ]
    assert RewardLedger.objects.filter(source_type="CAPTAIN_BONUS").count() == 2


@pytest.mark.django_db
def test_window_uses_latest_qualified_source_not_window_start(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, _, _, player, clan = clan_world
    cs = clan()
    start = window_start_for(now - timedelta(hours=2), 900)
    type(cs).objects.filter(pk=cs.pk).update(created_at=start - timedelta(minutes=1))
    cs.refresh_from_db()
    soldier_player = player(ClanRole.SOLDIER)
    member = ClanMembership.objects.create(
        player=soldier_player,
        season=cs.season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=start + timedelta(minutes=5),
        config_version=config,
    )
    activated(member)
    ClanActivity.objects.create(
        clan_season=cs,
        membership=member,
        sender=soldier_player,
        group=cs.clan.telegram_group,
        season=cs.season,
        config_version=config,
        telegram_message_id=901,
        telegram_update_id=901,
        observed_at=start + timedelta(minutes=10),
        character_count=150,
        rewardable_chars=150,
        classification="VALID",
        membership_eligible_at_source=True,
        home_integrity_at_source=True,
        context_owned_at_source=True,
        captain_bonus_eligible_at_source=True,
    )
    assert finalize_due_windows(now=start + timedelta(minutes=30)) == 1
    settlement = cs.economic_settlements.get()
    assert settlement.occurred_at == start + timedelta(minutes=10)
    assert settlement.membership_id == member.pk


@pytest.mark.django_db
def test_moderation_eligibility_is_historical(player_factory, phase4_config_factory) -> None:  # type: ignore[no-untyped-def]
    before = timezone.now() - timedelta(hours=1)
    after = timezone.now()
    config = phase4_config_factory(effective_at=before - timedelta(days=1))
    reporter, target = player_factory(88001), player_factory(88002)
    case = ModerationCase.objects.create(
        reporter=reporter, target=target, category="TEST", reason="test", config_version=config
    )
    action = ModerationAction.objects.create(
        case=case,
        system_actor="test",
        target=target,
        action_type="SUSPEND",
        reason="test",
        previous_state={"moderation_state": "ACTIVE"},
        resulting_state={"moderation_state": "SUSPENDED"},
        idempotency_key="historical-mod",
        suspension_expires_at=after + timedelta(days=30),
    )
    ModerationAction.objects.filter(pk=action.pk).update(created_at=after)
    target.moderation_state = Player.ModerationState.SUSPENDED
    target.save()
    assert reward_gameplay_allowed_at(target, before)
    assert not reward_gameplay_allowed_at(target, after + timedelta(seconds=1))


@pytest.mark.django_db
def test_mixed_captain_eligibility_window_uses_only_eligible_gross(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, _, _, player, clan = clan_world
    cs = clan()
    start = window_start_for(now - timedelta(hours=2), 900)
    type(cs).objects.filter(pk=cs.pk).update(created_at=start - timedelta(minutes=1))
    cs.refresh_from_db()
    soldier_player = player(ClanRole.SOLDIER)
    member = ClanMembership.objects.create(
        player=soldier_player,
        season=cs.season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=start,
        config_version=config,
    )
    activated(member)
    for message_id, eligible in [(910, True), (911, False)]:
        ClanActivity.objects.create(
            clan_season=cs,
            membership=member,
            sender=soldier_player,
            group=cs.clan.telegram_group,
            season=cs.season,
            config_version=config,
            telegram_message_id=message_id,
            telegram_update_id=message_id,
            observed_at=start + timedelta(minutes=message_id - 909),
            character_count=150,
            rewardable_chars=150,
            classification="VALID",
            membership_eligible_at_source=True,
            home_integrity_at_source=True,
            context_owned_at_source=True,
            captain_bonus_eligible_at_source=eligible,
        )
    assert finalize_due_windows(now=start + timedelta(minutes=30)) == 1
    daily = ClanCaptainBonusDaily.objects.get(clan_season=cs)
    assert daily.eligible_gross == 1  # 150 eligible chars / 150 chars per gross point.


@pytest.mark.django_db
def test_same_mission_source_can_progress_two_definitions(clan_world) -> None:  # type: ignore[no-untyped-def]
    now, config, _, resolved, _, clan = clan_world
    cs = clan()
    member = cs.memberships.get()
    activated(member)
    definitions = [
        ClanMissionDefinition.objects.create(
            key=f"parallel-{i}",
            version=1,
            kind="INDIVIDUAL",
            requirement="TEST",
            target_units=2,
            reward_band="SMALL",
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(hours=1),
            enabled=True,
            config_version=config,
        )
        for i in range(2)
    ]
    now = max(d.active_intervals.get().starts_at for d in definitions)
    rows = [
        record_mission_progress(
            definition=d,
            membership=member,
            source_id="canonical-event",
            units=1,
            occurred_at=now,
            resolved_config=resolved,
        )
        for d in definitions
    ]
    assert [row.units for row in rows] == [1, 1]


@pytest.mark.django_db
@pytest.mark.parametrize("transition", ["LEFT", "ACCESS_LOST", "SUSPENDED", "BANNED"])
def test_departed_group_participant_does_not_poison_completion(clan_world, transition) -> None:  # type: ignore[no-untyped-def]
    now, config, _, resolved, player, clan = clan_world
    cs = clan()
    captain = cs.memberships.get()
    activated(captain)
    old_player = player(ClanRole.SOLDIER)
    old = ClanMembership.objects.create(
        player=old_player,
        season=cs.season,
        clan_season=cs,
        role=ClanRole.SOLDIER,
        started_at=now - timedelta(minutes=2),
        config_version=config,
    )
    activated(old)
    definition = ClanMissionDefinition.objects.create(
        key=f"depart-{transition.lower()}",
        version=1,
        kind="GROUP",
        requirement="TEST",
        target_units=2,
        reward_band="GROUP",
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=1),
        enabled=True,
        config_version=config,
    )
    now = definition.active_intervals.get().starts_at
    record_mission_progress(
        definition=definition,
        membership=old,
        source_id="old",
        units=1,
        occurred_at=now,
        resolved_config=resolved,
    )
    completion = now + timedelta(seconds=1)
    if transition == "LEFT":
        old.ended_at = completion
        old.save()
    elif transition == "ACCESS_LOST":
        ClanMembershipEvent.objects.create(
            membership=old, kind="ACCESS_LOST", occurred_at=completion
        )
        old.access_state = "ACCESS_LOST"
        old.save()
    else:
        old_player.moderation_state = transition
        old_player.save()
    progress = record_mission_progress(
        definition=definition,
        membership=captain,
        source_id="complete",
        units=1,
        occurred_at=completion + timedelta(seconds=1),
        resolved_config=resolved,
    )
    old_participant = ClanMissionParticipant.objects.get(progress=progress, membership=old)
    active_participant = ClanMissionParticipant.objects.get(progress=progress, membership=captain)
    assert old_participant.reward_eligible is False and old_participant.reward_settlement is None
    assert active_participant.reward_eligible is True and active_participant.reward_settlement_id
    assert cs.hq_xp_entries.filter(source_type="CLAN_MISSION").count() == 1


@pytest.mark.django_db
def test_creator_ownership_history_resolves_delayed_events(player_factory) -> None:  # type: ignore[no-untyped-def]
    from apps.telegram.models import TelegramGroup

    now = timezone.now()
    creator = player_factory(88991)
    group = TelegramGroup.objects.create(
        telegram_chat_id=-88991,
        chat_type="supergroup",
        title="History",
        first_seen_at=now - timedelta(days=1),
        last_seen_at=now,
    )
    registration = CreatorGroupRegistration.objects.create(
        creator=creator, group=group, state="INACTIVE"
    )
    CreatorRegistrationStateEvent.objects.create(
        registration=registration,
        state="ACTIVE",
        reward_eligible=True,
        occurred_at=now - timedelta(hours=2),
        source="TEST_ACTIVE",
    )
    CreatorRegistrationStateEvent.objects.create(
        registration=registration,
        state="INACTIVE",
        reward_eligible=False,
        occurred_at=now - timedelta(hours=1),
        source="TEST_INACTIVE",
    )
    assert registration_reward_eligible_at(registration, now - timedelta(minutes=90))
    assert not registration_reward_eligible_at(registration, now - timedelta(hours=3))
    assert not registration_reward_eligible_at(registration, now)


@pytest.mark.django_db
def test_window_waits_for_maximum_qualification_horizon(  # type: ignore[no-untyped-def]
    clan_world, phase4_config_factory
) -> None:
    now, _, _, _, _, clan = clan_world
    config = phase4_config_factory(
        effective_at=window_start_for(now + timedelta(days=1), 900) + timedelta(seconds=900),
        version="phase8-horizon",
        mutate=lambda payload: payload["anti_fraud"]["clan"].update(
            exact_duplicate_window_seconds=100, near_duplicate_window_seconds=900
        ),
    )
    cs = clan()
    cs.config_version = config
    cs.save()
    policy = resolve_game_config_version(config.pk).clan
    assert policy is not None
    start = window_start_for(now - timedelta(hours=1), policy.activity.window_seconds)
    type(cs).objects.filter(pk=cs.pk).update(created_at=start - timedelta(minutes=1))
    cs.refresh_from_db()
    member = cs.memberships.get()
    member.config_version = config
    member.started_at = start
    member.save()
    activated(member)
    ClanActivity.objects.create(
        clan_season=cs,
        membership=member,
        sender=member.player,
        group=cs.clan.telegram_group,
        season=cs.season,
        config_version=config,
        telegram_message_id=990,
        telegram_update_id=990,
        observed_at=start + timedelta(minutes=1),
        character_count=150,
        rewardable_chars=150,
        classification="VALID",
        membership_eligible_at_source=True,
        home_integrity_at_source=True,
        context_owned_at_source=True,
        captain_bonus_eligible_at_source=True,
    )
    end = start + timedelta(seconds=policy.activity.window_seconds)
    assert finalize_due_windows(now=end + timedelta(seconds=500)) == 0
    assert finalize_due_windows(now=end + timedelta(seconds=901)) == 1
