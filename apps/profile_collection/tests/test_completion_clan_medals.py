"""Isolated synthetic membership/economic evidence, never Production backfill."""

# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from apps.clans.economy import settle_clan_reward
from apps.clans.models import (
    Clan,
    ClanMembership,
    ClanMembershipEvent,
    ClanRole,
    ClanSeason,
    ClanSeasonEvent,
)
from apps.clans.tests.test_phase8_concurrency import parallel
from apps.game_config.services import resolve_game_config_version
from apps.profile_collection.models import PlayerMedalAward
from apps.profile_collection.services import reconcile_profile, update_customization
from apps.rewards.models import RewardLedger
from apps.telegram.models import TelegramGroup


@pytest.fixture
def history(phase4_config_factory, season_factory, player_factory):
    now = timezone.now()
    start = now - timedelta(days=35)
    config = phase4_config_factory(effective_at=start - timedelta(days=1))
    season = season_factory(config, starts_at=start)
    resolved = resolve_game_config_version(config.pk)
    player = player_factory(940001)
    other = player_factory(940002)
    sequence = 0

    def clan(captain=player):
        nonlocal sequence
        sequence += 1
        group = TelegramGroup.objects.create(
            telegram_chat_id=-940000 - sequence,
            chat_type="supergroup",
            first_seen_at=start,
            last_seen_at=now,
        )
        identity = Clan.objects.create(
            telegram_group=group,
            name=f"Test {sequence}",
            tag=f"T{sequence}",
            normalized_tag=f"t{sequence}",
            creator=captain,
        )
        cs = ClanSeason.objects.create(
            clan=identity, season=season, captain=captain, base_capacity=20, config_version=config
        )
        ClanSeason.objects.filter(pk=cs.pk).update(created_at=start)
        cs.refresh_from_db()
        return cs

    def member(cs, begins=start, ends=None, role=ClanRole.CAPTAIN, who=player, activated=True):
        row = ClanMembership.objects.create(
            player=who,
            season=season,
            clan_season=cs,
            role=role,
            started_at=begins,
            ended_at=ends,
            config_version=config,
        )
        if activated:
            ClanMembershipEvent.objects.create(membership=row, kind="ACTIVATED", occurred_at=begins)
        return row

    return now, start, config, resolved, player, other, clan, member


def medals(world, at):
    return reconcile_profile(world[4], world[3], at)[3]


@pytest.mark.django_db
def test_loyalty_exact_boundary_historical_departure_and_zero_rewards(history):
    now, start, config, resolved, player, _, clan, member = history
    cs = clan()
    membership = member(cs, ends=start + timedelta(days=30))
    assert "clan-loyalty" not in medals(history, start + timedelta(days=30, microseconds=-1))
    award = medals(history, start + timedelta(days=30))["clan-loyalty"]
    assert award.source_season_id is None and award.config_version_id == config.pk
    assert award.evidence_metadata["membership_ids"] == [membership.pk]
    assert award.evidence_metadata["threshold_days"] == 30
    assert medals(history, now)["clan-loyalty"].pk == award.pk
    profile = update_customization(
        player,
        resolved,
        now,
        active_frame_key="standard",
        active_title_key="explorer",
        featured_medal_key="clan-loyalty",
        featured_achievement_key=None,
    )
    assert profile.featured_medal_key == "clan-loyalty"
    assert not RewardLedger.objects.exists()


@pytest.mark.django_db
def test_loyalty_rejoins_union_overlaps_and_excludes_access_gaps(history):
    _, start, _, _, _, _, clan, member = history
    cs = clan()
    first = member(cs, ends=start + timedelta(days=20))
    member(cs, begins=start + timedelta(days=10), ends=start + timedelta(days=25))
    member(cs, begins=start + timedelta(days=27))
    ClanMembershipEvent.objects.create(
        membership=first, kind="ACCESS_LOST", occurred_at=start + timedelta(days=2)
    )
    ClanMembershipEvent.objects.create(
        membership=first, kind="ACCESS_RESTORED", occurred_at=start + timedelta(days=4)
    )
    # Union = 23 days in [0,25), then 7 days from day 27: qualify exactly on day 34.
    assert "clan-loyalty" not in medals(history, start + timedelta(days=34, microseconds=-1))
    assert "clan-loyalty" in medals(history, start + timedelta(days=34))


@pytest.mark.django_db
def test_loyalty_does_not_combine_clans_or_unproven_time(history):
    now, start, _, _, _, _, clan, member = history
    first = clan()
    member(first, ends=start + timedelta(days=20))
    first.state = "ENDED"
    first.save()
    second = clan()
    member(second, begins=start + timedelta(days=20), ends=now)
    second.state = "ENDED"
    second.save()
    member(
        clan(), begins=start - timedelta(days=40), ends=start + timedelta(days=10), activated=False
    )
    assert "clan-loyalty" not in medals(history, now)


@pytest.mark.django_db
@pytest.mark.parametrize("end_event", ["CLAN_SEASON_DISSOLVED", "SEASON_CLOSE_ENDED"])
def test_loyalty_clips_to_season_and_dissolution(history, end_event):
    now, start, _, _, _, _, clan, member = history
    cs = clan()
    member(cs, begins=start - timedelta(days=30), ends=now)
    ClanSeasonEvent.objects.create(
        clan_season=cs, kind=end_event, occurred_at=start + timedelta(days=29)
    )
    assert "clan-loyalty" not in medals(history, now)


@pytest.mark.django_db
def test_loyalty_preserved_after_active_time_and_access_loss(history):
    now, start, _, _, _, _, clan, member = history
    cs = clan()
    m = member(cs)
    m.access_state = ClanMembership.Access.ACCESS_LOST
    m.access_lost_at = start + timedelta(days=30)
    m.save()
    ClanMembershipEvent.objects.create(
        membership=m, kind="ACCESS_LOST", occurred_at=m.access_lost_at
    )
    assert "clan-loyalty" in medals(history, now)


def hq(member, resolved, at, amount, key):
    return settle_clan_reward(
        membership=member,
        source_type="CLAN_ACTIVITY",
        source_id=key,
        gross_amount=amount,
        idempotency_key=f"TEST_ONLY:{key}",
        occurred_at=at,
        resolved_config=resolved,
    )


@pytest.mark.django_db
def test_leadership_exact_level_5_durable_sources_retry_and_departure(history):
    now, start, _, resolved, player, other, clan, member = history
    cs = clan()
    m = member(cs)
    # Isolated synthetic gross via canonical settlement: 5 Contribution -> 1 HQ XP.
    hq(m, resolved, start + timedelta(days=1), 74995, "hq-before")
    assert "leadership" not in medals(history, start + timedelta(days=1))
    settlement, created = hq(m, resolved, start + timedelta(days=2), 5, "hq-exact")
    assert created
    assert not hq(m, resolved, start + timedelta(days=2), 5, "hq-exact")[1]
    # No reconciliation until after departure and dissolution: historical evidence still qualifies.
    m.ended_at = start + timedelta(days=3)
    m.save()
    ClanSeasonEvent.objects.create(
        clan_season=cs, kind="CLAN_SEASON_DISSOLVED", occurred_at=m.ended_at
    )
    cs.state = ClanSeason.State.ENDED
    cs.save()
    before = RewardLedger.objects.count()
    award = medals(history, now)["leadership"]
    assert award.evidence_metadata["hq_xp"] == 15000
    assert award.evidence_metadata["captain_membership_ids"] == [m.pk]
    assert award.evidence_metadata["last_hq_entry_id"] == cs.hq_xp_entries.latest("pk").pk
    assert "leadership" not in reconcile_profile(other, resolved, now)[3]
    assert medals(history, now)["leadership"].pk == award.pk
    assert RewardLedger.objects.count() == before
    assert settlement.player_amount + settlement.treasury_amount == 5


@pytest.mark.django_db
def test_leadership_rejects_projection_only_role_choice_and_soldier(history):
    now, _, _, _, _, other, clan, member = history
    cs = clan(other)
    member(cs, role=ClanRole.SOLDIER)
    ClanSeason.objects.filter(pk=cs.pk).update(hq_level=5, hq_xp=15000)
    assert "leadership" not in medals(history, now)


@pytest.mark.django_db
def test_leadership_authority_loss_at_hq_threshold_then_restoration(history):
    now, start, _, resolved, _, _, clan, member = history
    cs = clan()
    m = member(cs)
    hq(m, resolved, start + timedelta(days=1), 75000, "hq-ready")
    # Exact threshold time is also authority-loss time: authoritative negative evidence wins.
    for kind in ("RECOVERY_ENTERED", "CAPTAIN_AUTHORITY_LOST"):
        ClanSeasonEvent.objects.create(
            clan_season=cs,
            kind=kind,
            source_type="CAPTAIN_AUTHORITY",
            occurred_at=start + timedelta(days=1),
        )
    assert "leadership" not in medals(history, start + timedelta(days=2))
    ClanSeasonEvent.objects.create(
        clan_season=cs, kind="CAPTAIN_AUTHORITY_RESTORED", occurred_at=start + timedelta(days=3)
    )
    assert (
        medals(history, now)["leadership"].evidence_metadata["qualified_at"]
        == (start + timedelta(days=3)).isoformat()
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_career_reconciliation_awards_once(history):
    now, start, _, resolved, player, _, clan, member = history
    cs = clan()
    m = member(cs)
    hq(m, resolved, start + timedelta(days=1), 75000, "hq-concurrent")
    results = parallel([lambda: reconcile_profile(player, resolved, now) for _ in range(2)])
    assert all(not isinstance(r, Exception) for r in results), results
    assert (
        PlayerMedalAward.objects.filter(
            player=player, medal_key__in=["clan-loyalty", "leadership"]
        ).count()
        == 2
    )


@pytest.mark.django_db
def test_independent_recovery_causes_and_open_vote_do_not_clear_with_authority(history):
    now, start, _, resolved, _, _, clan, member = history
    cs = clan()
    m = member(cs)
    hq(m, resolved, start + timedelta(days=1), 75000, "hq-recovery")
    for kind, source in [
        ("RECOVERY_ENTERED", "INACTIVITY"),
        ("RECOVERY_ENTERED", "CAPTAIN_AUTHORITY"),
        ("CAPTAIN_AUTHORITY_LOST", "TELEGRAM"),
    ]:
        ClanSeasonEvent.objects.create(
            clan_season=cs, kind=kind, source_type=source, occurred_at=start + timedelta(days=1)
        )
    ClanSeasonEvent.objects.create(
        clan_season=cs, kind="CAPTAIN_AUTHORITY_RESTORED", occurred_at=start + timedelta(days=2)
    )
    assert "leadership" not in medals(history, start + timedelta(days=3))
    ClanSeasonEvent.objects.create(
        clan_season=cs,
        kind="GOVERNANCE_OPENED",
        occurred_at=start + timedelta(days=4),
        metadata={"case_type": "RECOVERY_DISSOLUTION"},
    )
    ClanSeasonEvent.objects.create(
        clan_season=cs, kind="CAPTAIN_INACTIVITY_RECOVERED", occurred_at=start + timedelta(days=5)
    )
    assert "leadership" not in medals(history, now)


@pytest.mark.django_db
def test_access_lost_without_complete_event_history_cannot_inflate_loyalty(history):
    now, start, _, _, _, _, clan, member = history
    m = member(clan())
    m.access_state = "ACCESS_LOST"
    m.access_lost_at = start + timedelta(days=20)
    m.save()
    assert "clan-loyalty" not in medals(history, now)


@pytest.mark.django_db
def test_career_collection_http_reauthentication_and_customization(
    history, settings, client, init_data_factory, telegram_bot_token
):
    now, start, _, resolved, player, _, clan, member = history
    settings.TELEGRAM_BOT_TOKEN = telegram_bot_token
    cs = clan()
    hq(member(cs), resolved, start + timedelta(days=1), 75000, "hq-api")

    def auth():
        return {
            "HTTP_AUTHORIZATION": "tma " + init_data_factory(user={"id": player.telegram_user_id})
        }

    first = client.get("/api/v1/profile/collection", **auth())
    assert first.status_code == 200
    owned = {m["key"]: m for m in first.json()["medals"] if m["owned"]}
    assert {"clan-loyalty", "leadership"} <= owned.keys()
    update = client.put(
        "/api/v1/profile/customization",
        data={
            "active_frame": "standard",
            "active_title": "explorer",
            "featured_medal": "leadership",
        },
        content_type="application/json",
        **auth(),
    )
    assert update.status_code == 200
    refreshed = client.get("/api/v1/profile/collection", **auth())
    after = {m["key"]: m for m in refreshed.json()["medals"] if m["owned"]}
    assert after["leadership"]["featured"] is True
    assert after["leadership"]["awarded_at"] == owned["leadership"]["awarded_at"]
    assert after["clan-loyalty"]["awarded_at"] == owned["clan-loyalty"]["awarded_at"]
    Path("work").mkdir(exist_ok=True)
    Path("work/phase2-profile-http.json").write_text(json.dumps(refreshed.json(), indent=2))
