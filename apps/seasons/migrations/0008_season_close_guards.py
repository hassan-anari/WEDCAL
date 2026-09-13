from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION season_status_transition_guard() RETURNS trigger AS $$
BEGIN
 IF NEW.status IS DISTINCT FROM OLD.status
    AND current_setting('dexo.season_transition', true) IS DISTINCT FROM 'on' THEN
   RAISE EXCEPTION 'Season status transition requires service authorization';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS season_status_transition_guard_trigger ON seasons_season;
CREATE TRIGGER season_status_transition_guard_trigger BEFORE UPDATE ON seasons_season
FOR EACH ROW EXECUTE FUNCTION season_status_transition_guard();

CREATE OR REPLACE FUNCTION season_final_standing_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'Final Season standing is immutable'; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER season_final_player_immutable BEFORE UPDATE OR DELETE
ON seasons_playerseasonfinalstanding FOR EACH ROW
EXECUTE FUNCTION season_final_standing_immutable();
CREATE TRIGGER season_final_clan_immutable BEFORE UPDATE OR DELETE
ON seasons_clanseasonfinalstanding FOR EACH ROW EXECUTE FUNCTION season_final_standing_immutable();
"""
REVERSE = r"""
DROP TRIGGER IF EXISTS season_status_transition_guard_trigger ON seasons_season;
DROP TRIGGER IF EXISTS season_final_player_immutable ON seasons_playerseasonfinalstanding;
DROP TRIGGER IF EXISTS season_final_clan_immutable ON seasons_clanseasonfinalstanding;
DROP FUNCTION IF EXISTS season_status_transition_guard();
DROP FUNCTION IF EXISTS season_final_standing_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("seasons", "0007_clanseasonfinalstanding_playerseasonfinalstanding_and_more")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
