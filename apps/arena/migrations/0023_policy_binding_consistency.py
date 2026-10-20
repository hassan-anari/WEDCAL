# ruff: noqa: E501
"""A new-style binding must reproduce the GameConfig's explicitly selected policy."""

from django.db import migrations

FORWARD = r"""
CREATE FUNCTION arena_phase4_binding() RETURNS trigger AS $$
DECLARE config jsonb; policy arena_economicpolicyversion;
BEGIN
 SELECT c.payload INTO config FROM arena_economicepoch e
 JOIN game_config_gameconfigversion c ON c.id=e.config_version_id WHERE e.id=NEW.epoch_id;
 IF (config#>>'{meta,schema_version}')::int < 4 THEN RETURN NEW; END IF;
 SELECT * INTO policy FROM arena_economicpolicyversion WHERE id=NEW.policy_id;
 IF config#>'{dlx_distribution,economic_policy}' IS DISTINCT FROM
 jsonb_build_object('key',policy.key,'version',policy.version,'checksum',policy.checksum)
 OR policy.approval_reference='' OR (policy.payload->>'days')::int <>
 (config#>>'{dlx_distribution,timing,epoch_days}')::int
 THEN RAISE EXCEPTION 'Economic policy does not reproduce selected GameConfig binding'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_phase4_binding BEFORE INSERT ON arena_epochpolicybinding
 FOR EACH ROW EXECUTE FUNCTION arena_phase4_binding();

CREATE FUNCTION arena_phase4_epoch_identity() RETURNS trigger AS $$
DECLARE version int;
BEGIN
 SELECT c.schema_version INTO version FROM game_config_gameconfigversion c WHERE c.id=OLD.config_version_id;
 IF version>=4 AND (TG_OP='DELETE' OR
 (OLD.season_id,OLD.epoch_number,OLD.starts_at,OLD.ends_at,OLD.config_version_id,OLD.base_budget)
 IS DISTINCT FROM (NEW.season_id,NEW.epoch_number,NEW.starts_at,NEW.ends_at,NEW.config_version_id,NEW.base_budget))
 THEN RAISE EXCEPTION 'Configured Epoch commitments are immutable'; END IF;
 RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_phase4_epoch_identity BEFORE UPDATE OR DELETE ON arena_economicepoch
 FOR EACH ROW EXECUTE FUNCTION arena_phase4_epoch_identity();
"""
REVERSE = r"""
DROP TRIGGER arena_phase4_binding ON arena_epochpolicybinding;
DROP FUNCTION arena_phase4_binding();
DROP TRIGGER arena_phase4_epoch_identity ON arena_economicepoch;
DROP FUNCTION arena_phase4_epoch_identity();
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0022_growth_funded_caps_reserves")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
