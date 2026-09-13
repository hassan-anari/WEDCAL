from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION game_config_publication_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'DRAFT' AND NEW.status = 'PUBLISHED'
       AND current_setting('dexo.game_config_publish', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'GameConfig publication requires validated service authorization';
    END IF;
    IF OLD.status = 'PUBLISHED' AND (
       NEW.status IS DISTINCT FROM OLD.status OR
       NEW.version IS DISTINCT FROM OLD.version OR
       NEW.schema_version IS DISTINCT FROM OLD.schema_version OR
       NEW.effective_at IS DISTINCT FROM OLD.effective_at OR
       NEW.payload IS DISTINCT FROM OLD.payload OR
       NEW.payload_checksum IS DISTINCT FROM OLD.payload_checksum
    ) THEN
        RAISE EXCEPTION 'Published GameConfig snapshots are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS game_config_publication_guard_trigger ON game_config_gameconfigversion;
CREATE TRIGGER game_config_publication_guard_trigger
BEFORE UPDATE ON game_config_gameconfigversion
FOR EACH ROW EXECUTE FUNCTION game_config_publication_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS game_config_publication_guard_trigger
ON game_config_gameconfigversion;
DROP FUNCTION IF EXISTS game_config_publication_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("game_config", "0002_alter_gameconfigversion_options")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
