"""Fence correction admissions and protect bound normalized source identities from deletion."""

from django.db import migrations

TABLES = (
    "typing_game_typingattempt",
    "missions_playermission",
    "clans_claneconomicsettlement",
    "clans_clanactivity",
    "clans_clanactivitywindow",
    "network_networkactivity",
    "network_creatorwindowsettlement",
    "daily_dailyclaim",
)
FORWARD = r"""
CREATE OR REPLACE FUNCTION arena_s1_deferral_writer() RETURNS trigger AS $$
DECLARE season_key bigint;
BEGIN
 IF TG_TABLE_NAME='arena_economicdeferral' THEN
  SELECT season_id INTO season_key FROM arena_economicepoch WHERE id=NEW.epoch_id;
 ELSE
  SELECT e.season_id INTO season_key FROM arena_economicepoch e
  JOIN arena_economicdeferral d ON d.epoch_id=e.id WHERE d.id=NEW.deferral_id;
 END IF;
 PERFORM pg_advisory_xact_lock_shared(11042,season_key::int);
 IF current_setting('dexo.s1_source_writer',true) IS DISTINCT FROM 'S1_FINALIZED_V1'
 THEN RAISE EXCEPTION 'Economic deferral evidence requires compatible fenced writer'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_deferral_writer BEFORE INSERT ON arena_economicdeferral
 FOR EACH ROW EXECUTE FUNCTION arena_s1_deferral_writer();
CREATE TRIGGER arena_s1_proof_writer BEFORE INSERT ON arena_economicdeferralevidence
 FOR EACH ROW EXECUTE FUNCTION arena_s1_deferral_writer();

CREATE OR REPLACE FUNCTION arena_s1_source_delete_guard() RETURNS trigger AS $$
DECLARE season_key bigint;
BEGIN
 IF TG_TABLE_NAME='clans_claneconomicsettlement' THEN
  SELECT season_id INTO season_key FROM clans_clanseason WHERE id=OLD.clan_season_id;
 ELSE season_key:=(to_jsonb(OLD)->>'season_id')::bigint; END IF;
 IF EXISTS (SELECT 1 FROM arena_epochpolicybinding b JOIN arena_economicepoch e
 ON e.id=b.epoch_id WHERE e.season_id=season_key)
 THEN RAISE EXCEPTION 'Bound normalized economic source identities cannot be deleted'; END IF;
 RETURN OLD;
END; $$ LANGUAGE plpgsql;
"""
FORWARD += "\n".join(
    f"CREATE TRIGGER arena_s1_source_delete BEFORE DELETE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION arena_s1_source_delete_guard();"
    for table in TABLES
)
REVERSE = "\n".join(
    f"DROP TRIGGER IF EXISTS arena_s1_source_delete ON {table};" for table in TABLES
)
REVERSE += r"""
DROP TRIGGER IF EXISTS arena_s1_deferral_writer ON arena_economicdeferral;
DROP TRIGGER IF EXISTS arena_s1_proof_writer ON arena_economicdeferralevidence;
DROP FUNCTION IF EXISTS arena_s1_deferral_writer();
DROP FUNCTION IF EXISTS arena_s1_source_delete_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0018_phase3_assessment_revisions")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
