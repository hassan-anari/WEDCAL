# ruff: noqa: E501
"""Add guards without changing published payloads, allocations or historical policy identities."""

from django.db import migrations

FORWARD = r"""
CREATE FUNCTION arena_phase4_season_pin() RETURNS trigger AS $$
BEGIN
 IF OLD.status <> 'DRAFT' AND
 (OLD.baseline_config_id,OLD.starts_at,OLD.ends_at) IS DISTINCT FROM
 (NEW.baseline_config_id,NEW.starts_at,NEW.ends_at)
 THEN RAISE EXCEPTION 'Activated Season setup is immutable'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_phase4_season_pin BEFORE UPDATE ON seasons_season
 FOR EACH ROW EXECUTE FUNCTION arena_phase4_season_pin();

CREATE FUNCTION arena_phase4_epoch_setup() RETURNS trigger AS $$
DECLARE payload jsonb; timing jsonb; origin timestamptz; expected bigint; count_epochs int;
BEGIN
 SELECT c.payload,s.starts_at INTO payload,origin FROM seasons_season s
 JOIN game_config_gameconfigversion c ON c.id=s.baseline_config_id WHERE s.id=NEW.season_id;
 IF (payload#>>'{meta,schema_version}')::int < 4 THEN RETURN NEW; END IF;
 timing := payload#>'{dlx_distribution,timing}';
 count_epochs := (timing->>'epoch_count')::int;
 expected := (payload#>>'{dlx_distribution,pool,base_dlx_amount}')::bigint / count_epochs;
 IF NEW.epoch_number <= (payload#>>'{dlx_distribution,pool,base_dlx_amount}')::bigint % count_epochs
 THEN expected := expected+1; END IF;
 IF NEW.epoch_number NOT BETWEEN 1 AND count_epochs OR NEW.base_budget<>expected OR
 NEW.starts_at<>origin+make_interval(days=>(NEW.epoch_number-1)*(timing->>'epoch_days')::int) OR
 NEW.ends_at<>NEW.starts_at+make_interval(days=>(timing->>'epoch_days')::int) OR
 NEW.config_version_id<>(SELECT baseline_config_id FROM seasons_season WHERE id=NEW.season_id)
 THEN RAISE EXCEPTION 'Epoch differs from approved Season setup'; END IF;
 IF NEW.growth_budget<>COALESCE((SELECT sum(amount) FROM arena_growthepochfunding WHERE epoch_id=NEW.id),0)
 THEN RAISE EXCEPTION 'Growth budget must match durable funding'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_phase4_epoch_setup BEFORE INSERT OR UPDATE ON arena_economicepoch
 FOR EACH ROW EXECUTE FUNCTION arena_phase4_epoch_setup();

CREATE FUNCTION arena_phase4_growth_event() RETURNS trigger AS $$
DECLARE payload jsonb; max_pool numeric; base_pool numeric; spent numeric;
BEGIN
 PERFORM pg_advisory_xact_lock(11042,NEW.season_id::int);
 IF current_setting('dexo.phase4_growth',true) IS DISTINCT FROM 'CONFIGURABLE_WEEKLY_V1'
 THEN RAISE EXCEPTION 'Growth requires a compatible policy-aware writer'; END IF;
 SELECT c.payload INTO payload FROM seasons_season s JOIN game_config_gameconfigversion c
 ON c.id=s.baseline_config_id WHERE s.id=NEW.season_id AND c.id=NEW.config_version_id;
 max_pool := (payload#>>'{dlx_distribution,pool,max_dlx_amount}')::numeric;
 base_pool := (payload#>>'{dlx_distribution,pool,base_dlx_amount}')::numeric;
 SELECT COALESCE(sum(unlocked_amount),0) INTO spent FROM arena_growthunlockevent WHERE season_id=NEW.season_id;
 IF payload IS NULL OR NEW.unlocked_amount<0 OR base_pool+spent+NEW.unlocked_amount>max_pool
 THEN RAISE EXCEPTION 'Growth exceeds pinned authorization'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_phase4_growth_event BEFORE INSERT ON arena_growthunlockevent
 FOR EACH ROW EXECUTE FUNCTION arena_phase4_growth_event();

CREATE FUNCTION arena_phase4_funding() RETURNS trigger AS $$
DECLARE event arena_growthunlockevent; target arena_economicepoch; spent numeric;
BEGIN
 SELECT * INTO event FROM arena_growthunlockevent WHERE id=NEW.event_id FOR UPDATE;
 SELECT * INTO target FROM arena_economicepoch WHERE id=NEW.epoch_id FOR UPDATE;
 SELECT COALESCE(sum(amount),0) INTO spent FROM arena_growthepochfunding WHERE event_id=NEW.event_id;
 IF current_setting('dexo.phase4_growth',true) IS DISTINCT FROM 'CONFIGURABLE_WEEKLY_V1'
 OR target.season_id<>event.season_id OR target.state<>'OPEN'
 OR target.starts_at<(event.evidence->>'evaluated_at')::timestamptz
 OR EXISTS(SELECT 1 FROM arena_epochcategorybudget WHERE epoch_id=target.id)
 OR NEW.amount IS DISTINCT FROM (event.evidence->'placement'->>NEW.epoch_id::text)::bigint
 OR spent+NEW.amount>event.unlocked_amount
 THEN RAISE EXCEPTION 'Growth requires an exact approved mutable future placement'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_phase4_funding BEFORE INSERT ON arena_growthepochfunding
 FOR EACH ROW EXECUTE FUNCTION arena_phase4_funding();

CREATE FUNCTION arena_phase4_control() RETURNS trigger AS $$
BEGIN
 IF current_setting('dexo.phase4_control',true) IS DISTINCT FROM 'CONFIGURABLE_WEEKLY_V1'
 THEN RAISE EXCEPTION 'Automation authority requires explicit audited service'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_phase4_control BEFORE INSERT ON arena_economicautomationcontrol
 FOR EACH ROW EXECUTE FUNCTION arena_phase4_control();
"""
for name in ("growthunlockevent", "growthepochfunding", "economicautomationcontrol"):
    FORWARD += (
        f"CREATE TRIGGER arena_phase4_immutable BEFORE UPDATE OR DELETE ON arena_{name} "
        "FOR EACH ROW EXECUTE FUNCTION arena_reject_immutable_history();\n"
    )

REVERSE = """
DROP TRIGGER arena_phase4_season_pin ON seasons_season;
DROP TRIGGER arena_phase4_epoch_setup ON arena_economicepoch;
DROP TRIGGER arena_phase4_growth_event ON arena_growthunlockevent;
DROP TRIGGER arena_phase4_funding ON arena_growthepochfunding;
DROP TRIGGER arena_phase4_control ON arena_economicautomationcontrol;
DROP FUNCTION arena_phase4_season_pin();
DROP FUNCTION arena_phase4_epoch_setup();
DROP FUNCTION arena_phase4_growth_event();
DROP FUNCTION arena_phase4_funding();
DROP FUNCTION arena_phase4_control();
"""
for name in ("growthunlockevent", "growthepochfunding", "economicautomationcontrol"):
    REVERSE += f"DROP TRIGGER arena_phase4_immutable ON arena_{name};\n"


class Migration(migrations.Migration):
    dependencies = [("arena", "0020_configurable_seasons_operations")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
