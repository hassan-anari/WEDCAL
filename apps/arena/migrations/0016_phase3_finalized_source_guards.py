"""Seal normalized inputs once recognized; reject incompatible bound allocation writers."""

from django.db import migrations

TABLES = (
    "typing_game_typingattempt",
    "missions_playermission",
    "clans_claneconomicsettlement",
    "clans_clanactivity",
    "clans_clanactivitywindow",
    "network_networkactivity",
    "network_creatorwindowsettlement",
)

FORWARD = r"""
CREATE OR REPLACE FUNCTION arena_s1_finalized_source_guard() RETURNS trigger AS $$
DECLARE old_data jsonb; new_data jsonb; season_key bigint; terminal boolean;
BEGIN
 old_data:=to_jsonb(OLD); new_data:=to_jsonb(NEW);
 IF TG_TABLE_NAME='clans_claneconomicsettlement' THEN
  SELECT season_id INTO season_key FROM clans_clanseason WHERE id=OLD.clan_season_id;
 ELSE season_key:=(old_data->>'season_id')::bigint; END IF;
 IF NOT EXISTS (SELECT 1 FROM arena_epochpolicybinding b JOIN arena_economicepoch e
  ON e.id=b.epoch_id WHERE e.season_id=season_key) THEN RETURN NEW; END IF;
 terminal:=false;
 IF TG_TABLE_NAME='typing_game_typingattempt' THEN
  terminal:=old_data->>'status'='VALID';
 ELSIF TG_TABLE_NAME='missions_playermission' THEN
  terminal:=old_data->>'verified_at' IS NOT NULL;
  old_data:=old_data-ARRAY['status','claimed_at','reward_ledger_id','updated_at'];
  new_data:=new_data-ARRAY['status','claimed_at','reward_ledger_id','updated_at'];
 ELSIF TG_TABLE_NAME='clans_claneconomicsettlement' THEN
  terminal:=old_data->>'reward_id' IS NOT NULL;
 ELSIF TG_TABLE_NAME IN ('clans_clanactivity','network_networkactivity') THEN
  terminal:=(old_data->>'window_processed')::boolean;
  -- Retention can remove content; the existing writer guard restricts purge transitions.
  old_data:=old_data-ARRAY['text','exact_fingerprint','normalized_fingerprint'];
  new_data:=new_data-ARRAY['text','exact_fingerprint','normalized_fingerprint'];
 ELSE terminal:=old_data->>'finalized_at' IS NOT NULL;
 END IF;
 IF terminal AND old_data IS DISTINCT FROM new_data
 THEN RAISE EXCEPTION 'Finalized economic source semantics are immutable'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION arena_s1_allocation_writer_guard() RETURNS trigger AS $$
DECLARE season_key bigint; cap_amount bigint; minimum_score bigint;
BEGIN
 SELECT season_id INTO season_key FROM arena_economicepoch WHERE id=NEW.epoch_id;
 IF EXISTS (SELECT 1 FROM arena_epochpolicybinding b JOIN arena_economicepoch e
 ON e.id=b.epoch_id WHERE e.season_id=season_key) THEN
  PERFORM pg_advisory_xact_lock_shared(11042, season_key::int);
  IF current_setting('dexo.s1_source_writer',true) IS DISTINCT FROM 'S1_FINALIZED_V1'
   THEN RAISE EXCEPTION 'Bound allocations require compatible fenced writer'; END IF;
  SELECT budget.base_epoch_budget*(p.payload->>'player_cap_bps')::bigint
   /(p.payload->>'scale')::bigint, (p.payload->>'minimum_category_score')::bigint
  INTO cap_amount,minimum_score FROM arena_epochcategorybudget budget
  JOIN arena_epochpolicybinding b ON b.epoch_id=budget.epoch_id
  JOIN arena_economicpolicyversion p ON p.id=b.policy_id
  WHERE budget.epoch_id=NEW.epoch_id AND budget.category=NEW.category;
  IF cap_amount IS NULL OR NEW.amount>cap_amount OR NOT EXISTS
   (SELECT 1 FROM arena_epochperformancesnapshot s WHERE s.epoch_id=NEW.epoch_id
    AND s.category=NEW.category AND s.player_id=NEW.player_id
    AND s.performance_units>=minimum_score)
   THEN RAISE EXCEPTION 'Bound allocation exceeds cap or lacks eligible performance'; END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_allocation_writer BEFORE INSERT ON arena_dlxallocation
FOR EACH ROW EXECUTE FUNCTION arena_s1_allocation_writer_guard();

CREATE OR REPLACE FUNCTION arena_s1_referral_writer_guard() RETURNS trigger AS $$
DECLARE season_key bigint;
BEGIN
 IF NEW.status='VERIFIED' AND (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status) THEN
  SELECT e.season_id INTO season_key FROM arena_economicepoch e
  JOIN arena_epochpolicybinding b ON b.epoch_id=e.id
  WHERE NEW.verified_at>=e.starts_at AND NEW.verified_at<e.ends_at LIMIT 1;
  IF season_key IS NOT NULL THEN
   PERFORM pg_advisory_xact_lock_shared(11042,season_key::int);
   IF current_setting('dexo.s1_source_writer',true) IS DISTINCT FROM 'S1_FINALIZED_V1'
    THEN RAISE EXCEPTION 'Bound referral activation requires compatible fenced writer'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_referral_writer BEFORE INSERT OR UPDATE ON referrals_referralrelation
FOR EACH ROW EXECUTE FUNCTION arena_s1_referral_writer_guard();
"""
FORWARD += "\n".join(
    f"CREATE TRIGGER arena_s1_finalized_{table} BEFORE UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION arena_s1_finalized_source_guard();"
    for table in TABLES
)
REVERSE = (
    "\n".join(f"DROP TRIGGER IF EXISTS arena_s1_finalized_{table} ON {table};" for table in TABLES)
    + r"""
DROP TRIGGER IF EXISTS arena_s1_allocation_writer ON arena_dlxallocation;
DROP TRIGGER IF EXISTS arena_s1_referral_writer ON referrals_referralrelation;
DROP FUNCTION IF EXISTS arena_s1_finalized_source_guard();
DROP FUNCTION IF EXISTS arena_s1_allocation_writer_guard();
DROP FUNCTION IF EXISTS arena_s1_referral_writer_guard();
"""
)


class Migration(migrations.Migration):
    dependencies = [("arena", "0015_phase3_source_provenance_guards")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
