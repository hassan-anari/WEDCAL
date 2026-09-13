from typing import Any

from django.db import migrations
from django.db.models import Count, Max


def backfill_tie_provenance(apps: Any, schema_editor: Any) -> None:
    PlayerSeason = apps.get_model("seasons", "PlayerSeason")
    RewardLedger = apps.get_model("rewards", "RewardLedger")
    ClanSeason = apps.get_model("clans", "ClanSeason")
    ClanEconomicSettlement = apps.get_model("clans", "ClanEconomicSettlement")
    RankingProjectionEvent = apps.get_model("arena", "RankingProjectionEvent")

    for row in RewardLedger.objects.values("player_id", "season_id").annotate(
        reached=Max("occurred_at"), generation=Count("id")
    ):
        PlayerSeason.objects.filter(
            player_id=row["player_id"], season_id=row["season_id"], season_points__gt=0
        ).update(score_reached_at=row["reached"], ranking_generation=row["generation"])
    for row in (
        ClanEconomicSettlement.objects.filter(clan_score_amount__gt=0)
        .values("clan_season_id")
        .annotate(reached=Max("occurred_at"), generation=Count("id"))
    ):
        ClanSeason.objects.filter(pk=row["clan_season_id"], competitive_score__gt=0).update(
            score_reached_at=row["reached"], ranking_generation=row["generation"]
        )
    for projection in PlayerSeason.objects.filter(
        season_points__gt=0, score_reached_at__isnull=True
    ):
        PlayerSeason.objects.filter(pk=projection.pk).update(
            score_reached_at=projection.updated_at, ranking_generation=1
        )
    for projection in ClanSeason.objects.filter(
        competitive_score__gt=0, score_reached_at__isnull=True
    ):
        ClanSeason.objects.filter(pk=projection.pk).update(
            score_reached_at=projection.updated_at, ranking_generation=1
        )
    generations: dict[tuple[str, int, int], int] = {}
    for event in RankingProjectionEvent.objects.order_by("id"):
        key = (event.entity_type, event.season_id, event.entity_id)
        generations[key] = generations.get(key, 0) + 1
        RankingProjectionEvent.objects.filter(pk=event.pk).update(generation=generations[key])


FORWARD = """
CREATE OR REPLACE FUNCTION arena_reject_immutable_history()
RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'Arena economic history is immutable'; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER arena_qualification_immutable BEFORE UPDATE OR DELETE ON arena_qualificationdecision
FOR EACH ROW EXECUTE FUNCTION arena_reject_immutable_history();
CREATE TRIGGER arena_snapshot_immutable BEFORE UPDATE OR DELETE ON arena_epochperformancesnapshot
FOR EACH ROW EXECUTE FUNCTION arena_reject_immutable_history();
CREATE OR REPLACE FUNCTION arena_protect_snapshot_insert()
RETURNS trigger AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM arena_economicepoch e WHERE e.id = NEW.epoch_id
                 AND e.state = 'DRAINING')
    THEN RAISE EXCEPTION 'Snapshots may only be inserted while draining'; END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_snapshot_insert_boundary BEFORE INSERT ON arena_epochperformancesnapshot
FOR EACH ROW EXECUTE FUNCTION arena_protect_snapshot_insert();

CREATE OR REPLACE FUNCTION arena_protect_budget_inputs()
RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Frozen budget history is immutable'; END IF;
  IF EXISTS (SELECT 1 FROM arena_economicepoch e WHERE e.id = OLD.epoch_id
             AND e.state IN ('FROZEN','SETTLING','SETTLED'))
     AND (OLD.epoch_id, OLD.category, OLD.weight_bps, OLD.base_epoch_budget,
          OLD.growth_contribution, OLD.config_version_id)
         IS DISTINCT FROM
         (NEW.epoch_id, NEW.category, NEW.weight_bps, NEW.base_epoch_budget,
          NEW.growth_contribution, NEW.config_version_id)
  THEN RAISE EXCEPTION 'Frozen budget inputs are immutable'; END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_budget_inputs_immutable BEFORE UPDATE OR DELETE ON arena_epochcategorybudget
FOR EACH ROW EXECUTE FUNCTION arena_protect_budget_inputs();

CREATE OR REPLACE FUNCTION arena_protect_epoch_history()
RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND OLD.state IN ('FROZEN','SETTLING','SETTLED')
    THEN RAISE EXCEPTION 'Frozen epoch history is immutable'; END IF;
  IF TG_OP = 'UPDATE' AND OLD.state = 'SETTLED'
    THEN RAISE EXCEPTION 'Settled epoch history is immutable'; END IF;
  IF TG_OP = 'UPDATE' AND OLD.state IN ('FROZEN','SETTLING')
     AND (OLD.season_id, OLD.epoch_number, OLD.starts_at, OLD.ends_at, OLD.config_version_id,
          OLD.base_budget, OLD.growth_budget, OLD.freeze_watermark, OLD.frozen_at,
          OLD.snapshots_completed_at)
         IS DISTINCT FROM
         (NEW.season_id, NEW.epoch_number, NEW.starts_at, NEW.ends_at, NEW.config_version_id,
          NEW.base_budget, NEW.growth_budget, NEW.freeze_watermark, NEW.frozen_at,
          NEW.snapshots_completed_at)
    THEN RAISE EXCEPTION 'Frozen epoch inputs are immutable'; END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_epoch_history_immutable BEFORE UPDATE OR DELETE ON arena_economicepoch
FOR EACH ROW EXECUTE FUNCTION arena_protect_epoch_history();

CREATE OR REPLACE FUNCTION arena_protect_batch_history()
RETURNS trigger AS $$
BEGIN
  IF OLD.confirmed_at IS NOT NULL THEN RAISE EXCEPTION 'Confirmed batch is immutable'; END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_batch_history_immutable BEFORE UPDATE OR DELETE ON arena_allocationbatch
FOR EACH ROW EXECUTE FUNCTION arena_protect_batch_history();
"""

REVERSE = """
DROP TRIGGER IF EXISTS arena_qualification_immutable ON arena_qualificationdecision;
DROP TRIGGER IF EXISTS arena_snapshot_immutable ON arena_epochperformancesnapshot;
DROP TRIGGER IF EXISTS arena_snapshot_insert_boundary ON arena_epochperformancesnapshot;
DROP TRIGGER IF EXISTS arena_budget_inputs_immutable ON arena_epochcategorybudget;
DROP TRIGGER IF EXISTS arena_epoch_history_immutable ON arena_economicepoch;
DROP TRIGGER IF EXISTS arena_batch_history_immutable ON arena_allocationbatch;
DROP FUNCTION IF EXISTS arena_reject_immutable_history();
DROP FUNCTION IF EXISTS arena_protect_snapshot_insert();
DROP FUNCTION IF EXISTS arena_protect_budget_inputs();
DROP FUNCTION IF EXISTS arena_protect_epoch_history();
DROP FUNCTION IF EXISTS arena_protect_batch_history();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("arena", "0003_economicepoch_snapshots_completed_at_and_more"),
        ("seasons", "0004_playerseason_ranking_generation"),
        ("clans", "0013_clanseason_ranking_generation"),
        ("rewards", "0005_rewardledger_append_only"),
    ]
    operations = [
        migrations.RunPython(backfill_tie_provenance, migrations.RunPython.noop),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
