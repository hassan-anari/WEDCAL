from django.db import migrations

FORWARD = r"""
CREATE TRIGGER arena_growth_evaluation_immutable BEFORE UPDATE OR DELETE
ON arena_growthevaluation FOR EACH ROW EXECUTE FUNCTION arena_reject_immutable_history();
CREATE FUNCTION arena_phase4_growth_conservation() RETURNS trigger AS $$
BEGIN
 IF NEW.unlocked_amount<>(SELECT COALESCE(sum(amount),0) FROM arena_growthepochfunding
 WHERE event_id=NEW.id)
 THEN RAISE EXCEPTION 'Every authorized Growth unit requires immutable receiving funding'; END IF;
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER arena_growth_conservation AFTER INSERT ON arena_growthunlockevent
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION arena_phase4_growth_conservation();
"""
REVERSE = r"""
DROP TRIGGER arena_growth_evaluation_immutable ON arena_growthevaluation;
DROP TRIGGER arena_growth_conservation ON arena_growthunlockevent;
DROP FUNCTION arena_phase4_growth_conservation();
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0026_growth_evaluation_audit")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
