from django.db import migrations

SQL = """
CREATE FUNCTION dexo_qa_clan_grant_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'QA recreation grants are retained after consumption';
    END IF;
    IF (NEW.player_id, NEW.season_id, NEW.granted_by_id, NEW.reason,
        NEW.ticket_reference, NEW.granted_at) IS DISTINCT FROM
       (OLD.player_id, OLD.season_id, OLD.granted_by_id, OLD.reason,
        OLD.ticket_reference, OLD.granted_at) THEN
        RAISE EXCEPTION 'QA recreation grant identity is immutable';
    END IF;
    IF OLD.consumed_at IS NOT NULL AND
       (NEW.consumed_at, NEW.consumed_clan_season_id) IS DISTINCT FROM
       (OLD.consumed_at, OLD.consumed_clan_season_id) THEN
        RAISE EXCEPTION 'QA recreation grant cannot be consumed again or reset';
    END IF;
    IF NEW.consumed_clan_season_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM clans_clanseason cs WHERE cs.id = NEW.consumed_clan_season_id
        AND cs.captain_id = NEW.player_id AND cs.season_id = NEW.season_id
    ) THEN
        RAISE EXCEPTION 'QA grant consumption must match its player and Season';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER dexo_qa_clan_grant_history
BEFORE UPDATE OR DELETE ON clans_clanrecreationgrant
FOR EACH ROW EXECUTE FUNCTION dexo_qa_clan_grant_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("clans", "0020_qa_recreation_grant")]
    operations = [
        migrations.RunSQL(
            SQL,
            "DROP TRIGGER dexo_qa_clan_grant_history ON clans_clanrecreationgrant; "
            "DROP FUNCTION dexo_qa_clan_grant_guard();",
        )
    ]
