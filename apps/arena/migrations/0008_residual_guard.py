from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION arena_residual_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'Season DLX residual event is immutable'; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER arena_residual_immutable_trigger BEFORE UPDATE OR DELETE
ON arena_seasondlxresidualevent FOR EACH ROW EXECUTE FUNCTION arena_residual_immutable();
"""
REVERSE = r"""
DROP TRIGGER IF EXISTS arena_residual_immutable_trigger ON arena_seasondlxresidualevent;
DROP FUNCTION IF EXISTS arena_residual_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0007_seasondlxresidualevent")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
