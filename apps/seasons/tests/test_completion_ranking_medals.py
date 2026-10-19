"""Owner-finalized percentile rules, using durable standings rather than Redis."""

# mypy: disable-error-code="no-untyped-def"
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.profile_collection.models import PlayerMedalAward
from apps.rewards.models import RewardLedger
from apps.seasons.close import award_season_honors, freeze_standings
from apps.seasons.models import PlayerSeason, PlayerSeasonFinalStanding, Season


@pytest.fixture
def rankings(phase4_config_factory, season_factory, player_factory):
    config = phase4_config_factory()
    season = season_factory(config)

    def populate(population):
        at = season.starts_at + timedelta(days=1)
        players = [player_factory(920000 + n) for n in range(population + 1)]
        PlayerSeason.objects.bulk_create(
            [
                PlayerSeason(
                    player=p,
                    season=season,
                    season_points=population - n,
                    score_reached_at=at if n < population else None,
                )
                for n, p in enumerate(players)
            ]
        )
        while not freeze_standings(season, batch_size=17)["complete"]:
            pass
        return players

    return config, season, populate


@pytest.mark.django_db
@pytest.mark.parametrize("population", [0, 1, 9, 10, 11, 19, 20, 99, 100, 101, 200])
def test_percentile_boundaries_and_small_populations(rankings, population):
    config, season, populate = rankings
    players = populate(population)
    while award_season_honors(season, limit=13)["remaining"]:
        pass
    assert award_season_honors(season) == {"created": 0, "remaining": 0}
    for key, count in [
        ("season-top-10", population // 10),
        ("season-top-1", population // 100),
        ("season-champion", min(population, 1)),
    ]:
        awarded = set(
            PlayerMedalAward.objects.filter(source_season=season, medal_key=key).values_list(
                "player_id", flat=True
            )
        )
        assert awarded == {p.pk for p in players[:count]}
    assert not PlayerMedalAward.objects.filter(player=players[-1]).exists()
    assert set(PlayerMedalAward.objects.values_list("config_version_id", flat=True)) <= {config.pk}
    assert not RewardLedger.objects.exists()


@pytest.mark.django_db
def test_prior_season_founding_cannot_hide_current_season_honors(rankings):
    config, season, populate = rankings
    player = populate(10)[0]
    earlier = Season.objects.create(
        code="earlier",
        title="Earlier",
        baseline_config=config,
        starts_at=season.starts_at - timedelta(days=45),
        ends_at=season.starts_at,
    )
    old = PlayerMedalAward.objects.create(
        player=player,
        source_season=earlier,
        medal_key="season-founding",
        awarded_at=earlier.ends_at,
        config_version=config,
        reason="historical",
    )
    # A valid award already exists in this Season, but its founding medal is in another Season.
    PlayerMedalAward.objects.create(
        player=player,
        source_season=season,
        medal_key="season-participant",
        awarded_at=timezone.now(),
        config_version=config,
        reason="preserved",
    )
    award_season_honors(season)
    assert PlayerMedalAward.objects.filter(
        player=player, source_season=season, medal_key="season-champion"
    ).exists()
    old.refresh_from_db()
    assert old.reason == "historical"


@pytest.mark.django_db
def test_final_freeze_ties_use_time_then_player_id(rankings, monkeypatch):
    from apps.seasons import close
    from apps.seasons.models import SeasonCloseRun

    _, season, populate = rankings
    players = populate(0)
    at = season.starts_at + timedelta(days=1)
    # Populate manually so insertion order differs from canonical time/ID order.
    from apps.players.models import Player

    for number in range(20):
        p = Player.objects.create(
            telegram_user_id=930000 + number,
            first_seen_at=at,
            last_seen_at=at,
            first_seen_source="MINI_APP",
        )
        players.append(p)
        PlayerSeason.objects.create(
            player=p,
            season=season,
            season_points=100,
            score_reached_at=at + timedelta(seconds=1 if number == 0 else 0),
        )
    monkeypatch.setattr(close, "_enqueue", lambda *_a, **_kw: None)
    run = SeasonCloseRun.objects.create(
        season=season,
        idempotency_key="TEST_ONLY:tie-freeze",
        stage="FREEZING_RANKINGS",
        cutoff_at=season.ends_at,
        service_actor="isolated-test",
        reason="Verify no honors before every frozen rank exists",
    )
    while close.advance_close(run.pk, batch_size=7) == "FREEZING_RANKINGS":
        assert not PlayerMedalAward.objects.exists()
    assert not PlayerMedalAward.objects.exists()
    assert list(
        PlayerSeasonFinalStanding.objects.filter(season=season)
        .order_by("final_rank")
        .values_list("player_id", flat=True)
    ) == [p.pk for p in players[2:]] + [players[1].pk]
    assert close.advance_close(run.pk, batch_size=100) == "FINALIZING"
    assert set(
        PlayerMedalAward.objects.filter(medal_key="season-top-10").values_list(
            "player_id", flat=True
        )
    ) == {players[2].pk, players[3].pk}
    assert PlayerMedalAward.objects.get(medal_key="season-champion").player_id == players[2].pk


@pytest.mark.django_db(transaction=True)
def test_interrupted_award_stage_rolls_back_batch_and_resumes(rankings, monkeypatch):
    from apps.seasons import close
    from apps.seasons.models import SeasonCloseRun

    config, season, populate = rankings
    populate(20)
    monkeypatch.setattr(close, "_enqueue", lambda *_a, **_kw: None)
    # Isolated stage fixture: final rankings above are complete. Real stage transactions follow.
    run = SeasonCloseRun.objects.create(
        season=season,
        idempotency_key="TEST_ONLY:honors-resume",
        stage="AWARDING_HONORS",
        cutoff_at=season.ends_at,
        service_actor="isolated-test",
        reason="Injected worker loss after a committed award batch",
    )
    assert close.advance_close(run.pk, batch_size=3) == "AWARDING_HONORS"
    preserved = list(PlayerMedalAward.objects.order_by("pk").values())
    standings = list(PlayerSeasonFinalStanding.objects.order_by("pk").values())
    original = close.award_season_honors

    def fail_after_writes(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected honor worker loss")

    monkeypatch.setattr(close, "award_season_honors", fail_after_writes)
    with pytest.raises(RuntimeError, match="worker loss"):
        close.advance_close(run.pk, batch_size=3)
    run.refresh_from_db()
    assert run.stage == "FAILED" and run.failure_stage == "AWARDING_HONORS"
    assert list(PlayerMedalAward.objects.order_by("pk").values()) == preserved
    resumed = close.start_or_resume_close(
        season, actor=None, service_actor="isolated-test", reason="Resume failed honor batch"
    )
    assert resumed.stage == "AWARDING_HONORS" and resumed.retry_count == 1
    monkeypatch.setattr(close, "award_season_honors", original)
    while close.advance_close(run.pk, batch_size=3) == "AWARDING_HONORS":
        pass
    run.refresh_from_db()
    assert run.stage == "FINALIZING"
    assert (
        PlayerMedalAward.objects.filter(source_season=season, medal_key="season-founding").count()
        == 20
    )
    assert (
        PlayerMedalAward.objects.filter(source_season=season, medal_key="season-top-10").count()
        == 2
    )
    assert (
        PlayerMedalAward.objects.filter(source_season=season, medal_key="season-top-1").count() == 0
    )
    assert list(PlayerSeasonFinalStanding.objects.order_by("pk").values()) == standings
    assert (
        PlayerMedalAward.objects.filter(config_version=config).count()
        == PlayerMedalAward.objects.count()
    )
    assert original(season) == {"created": 0, "remaining": 0}
