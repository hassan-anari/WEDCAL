from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION season_settlement_control_guard() RETURNS trigger AS $$
BEGIN
 IF NEW.settlement_start_epoch IS DISTINCT FROM OLD.settlement_start_epoch
    AND current_setting('dexo.settlement_control', true) IS DISTINCT FROM 'on' THEN
   RAISE EXCEPTION 'Settlement control requires audited service authorization';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS season_settlement_control_guard_trigger ON seasons_season;
CREATE TRIGGER season_settlement_control_guard_trigger BEFORE UPDATE ON seasons_season
FOR EACH ROW EXECUTE FUNCTION season_settlement_control_guard();
"""
REVERSE = """
DROP TRIGGER IF EXISTS season_settlement_control_guard_trigger ON seasons_season;
DROP FUNCTION IF EXISTS season_settlement_control_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("seasons", "0005_alter_seasonsettlementcontrolevent_options_and_more")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
