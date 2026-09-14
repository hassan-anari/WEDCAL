from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION season_close_barrier_guard() RETURNS trigger AS $$
BEGIN
 IF NEW.barrier_established_at IS DISTINCT FROM OLD.barrier_established_at
    AND current_setting('dexo.season_close_barrier', true) IS DISTINCT FROM 'on' THEN
   RAISE EXCEPTION 'Season close barrier requires service authorization';
 END IF;
 IF OLD.barrier_established_at IS NOT NULL
    AND NEW.barrier_established_at IS DISTINCT FROM OLD.barrier_established_at THEN
   RAISE EXCEPTION 'Established Season close barrier is immutable';
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER season_close_barrier_guard_trigger BEFORE UPDATE ON seasons_seasoncloserun
FOR EACH ROW EXECUTE FUNCTION season_close_barrier_guard();
"""
REVERSE = r"""
DROP TRIGGER IF EXISTS season_close_barrier_guard_trigger ON seasons_seasoncloserun;
DROP FUNCTION IF EXISTS season_close_barrier_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("seasons", "0009_seasoncloserun_barrier_established_at_and_more"),
    ]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
