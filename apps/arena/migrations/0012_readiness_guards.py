from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION arena_readiness_history_guard() RETURNS trigger AS $$
BEGIN
 IF TG_OP = 'DELETE' OR OLD.status <> 'RUNNING' THEN
   RAISE EXCEPTION 'Terminal Epoch readiness evidence is immutable';
 END IF;
 IF (OLD.epoch_id, OLD.evaluation_key, OLD.policy_key, OLD.policy_version,
     OLD.config_version_id, OLD.cutoff_at, OLD.created_at) IS DISTINCT FROM
    (NEW.epoch_id, NEW.evaluation_key, NEW.policy_key, NEW.policy_version,
     NEW.config_version_id, NEW.cutoff_at, NEW.created_at) THEN
   RAISE EXCEPTION 'Epoch readiness provenance is immutable';
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_readiness_history BEFORE UPDATE OR DELETE ON arena_epochreadinessevaluation
FOR EACH ROW EXECUTE FUNCTION arena_readiness_history_guard();

CREATE OR REPLACE FUNCTION arena_require_freeze_readiness() RETURNS trigger AS $$
BEGIN
 IF NEW.state IN ('SETTLING', 'SETTLED') AND
    (TG_OP = 'INSERT' OR OLD.state IN ('OPEN', 'DRAINING')) THEN
   RAISE EXCEPTION 'Epoch cannot bypass authoritative Freeze';
 END IF;
 IF NEW.state = 'FROZEN' AND (TG_OP = 'INSERT' OR OLD.state <> 'FROZEN') THEN
   IF current_setting('dexo.epoch_freeze', true) IS DISTINCT FROM 'on' OR NOT EXISTS (
     SELECT 1 FROM arena_epochreadinessevaluation r
     WHERE r.id::text = NEW.freeze_watermark->>'readiness_evaluation_id'
       AND r.epoch_id = NEW.id AND r.status = 'COMPLETE'
       AND r.config_version_id = NEW.config_version_id AND r.cutoff_at = NEW.ends_at
       AND r.completed_at IS NOT NULL AND r.input_digest <> ''
       AND r.evaluator_version <> '' AND r.blocking_reasons = '[]'::jsonb
       AND r.policy_key = (SELECT payload #>>
         '{dlx_distribution,pool,growth_unlock,qualified_player_predicate,policy_key}'
         FROM game_config_gameconfigversion WHERE id=NEW.config_version_id)
       AND r.policy_version::text = (SELECT payload #>>
         '{dlx_distribution,pool,growth_unlock,qualified_player_predicate,policy_version}'
         FROM game_config_gameconfigversion WHERE id=NEW.config_version_id)
       AND r.input_digest = NEW.freeze_watermark->>'input_digest'
   ) THEN
     RAISE EXCEPTION 'Authoritative Epoch readiness required before Freeze';
   END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_require_freeze_readiness_trigger BEFORE INSERT OR UPDATE ON arena_economicepoch
FOR EACH ROW EXECUTE FUNCTION arena_require_freeze_readiness();
"""
REVERSE = r"""
DROP TRIGGER IF EXISTS arena_require_freeze_readiness_trigger ON arena_economicepoch;
DROP FUNCTION IF EXISTS arena_require_freeze_readiness();
DROP TRIGGER IF EXISTS arena_readiness_history ON arena_epochreadinessevaluation;
DROP FUNCTION IF EXISTS arena_readiness_history_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0011_epoch_readiness_evaluation")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
