# ruff: noqa: E501
"""Additive guards. Phase 1 Freeze/readiness/history triggers remain installed unchanged."""

from django.db import migrations

IMMUTABLE = (
    "economicpolicyversion",
    "epochpolicybinding",
    "economicrecognition",
    "economicdeferral",
    "economicdeferralresolution",
    "economicreviewdecision",
    "epochsourcemanifest",
    "categorycorrectionreserve",
    "correctionassessment",
    "dlxcorrectionround",
    "dlxcorrectionentitlement",
    "dlxcorrection",
    "dlxcorrectionattempt",
    "dlxcorrectionadjustment",
)
SOURCES = (
    "arena_performancesourcefact",
    "daily_dailyclaim",
    "missions_playermission",
    "typing_game_typingattempt",
    "clans_clancontributionentry",
    "clans_claneconomicsettlement",
    "clans_clanactivity",
    "clans_clanactivitywindow",
    "network_networkactivity",
    "network_creatorwindowsettlement",
    "rewards_rewardledger",
)

FORWARD = r"""
CREATE OR REPLACE FUNCTION arena_s1_recognition_boundary() RETURNS trigger AS $$
DECLARE expected_category text; expected_component text;
BEGIN
 PERFORM pg_advisory_xact_lock_shared(11042, (SELECT season_id::int FROM arena_economicepoch WHERE id=NEW.epoch_id));
 SELECT category,component INTO expected_category,expected_component FROM (VALUES
 ('BASE_TAP_BATCH','CORE_GAMEPLAY','tap'), ('BASE_TAP_V2_BATCH','CORE_GAMEPLAY','tap'),
 ('TYPING_CREDIT','CORE_GAMEPLAY','typing'), ('DAILY_CLAIM','CORE_GAMEPLAY','daily'),
 ('PERSONAL_MISSION_VERIFIED','CORE_GAMEPLAY','mission'),
 ('CLAN_CONTRIBUTION','CLAN_PERFORMANCE','contribution'),
 ('CREATOR_ACTIVITY','CREATOR_COMMUNITY_GROWTH','speaker'),
 ('REFERRAL_ACTIVATION','VERIFIED_REFERRAL','acquisition'),
 ('COMMUNITY_ACTIVITY','LOYALTY_CONSISTENCY','presence')
 ) AS mapping(source,category,component) WHERE source=NEW.source_type;
 IF expected_category IS NULL OR NEW.category<>expected_category OR NEW.component<>expected_component
 OR NOT EXISTS (SELECT 1 FROM arena_economicepoch e JOIN arena_epochpolicybinding b ON b.epoch_id=e.id
  WHERE e.id=NEW.epoch_id AND e.state IN ('OPEN','DRAINING') AND b.policy_id=NEW.policy_id
   AND NEW.recognized_at>=e.starts_at AND NEW.recognized_at<e.ends_at)
 THEN RAISE EXCEPTION 'Recognition requires matching bound policy, source allowlist and open Epoch'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_recognition_insert BEFORE INSERT ON arena_economicrecognition
FOR EACH ROW EXECUTE FUNCTION arena_s1_recognition_boundary();

CREATE OR REPLACE FUNCTION arena_s1_source_writer_guard() RETURNS trigger AS $$
DECLARE data jsonb; season_key bigint;
BEGIN
 data := to_jsonb(NEW);
 IF TG_TABLE_NAME='clans_clancontributionentry' THEN
  SELECT season_id INTO season_key FROM clans_clanmembership WHERE id=NEW.membership_id;
 ELSIF TG_TABLE_NAME='clans_claneconomicsettlement' THEN
  SELECT season_id INTO season_key FROM clans_clanseason WHERE id=NEW.clan_season_id;
 ELSE season_key := (data->>'season_id')::bigint;
 END IF;
 IF season_key IS NOT NULL AND EXISTS (SELECT 1 FROM arena_epochpolicybinding b
 JOIN arena_economicepoch e ON e.id=b.epoch_id WHERE e.season_id=season_key) THEN
  -- Source retention purge cannot change performance semantics and remains permitted.
  IF TG_OP='UPDATE' AND TG_TABLE_NAME='network_networkactivity'
   AND (to_jsonb(OLD)-ARRAY['text','exact_fingerprint','normalized_fingerprint']) =
       (data-ARRAY['text','exact_fingerprint','normalized_fingerprint'])
   AND (data->>'text' IS NULL OR data->>'text' IS NOT DISTINCT FROM to_jsonb(OLD)->>'text')
   AND (data->>'exact_fingerprint'='' OR data->>'exact_fingerprint'=to_jsonb(OLD)->>'exact_fingerprint')
   AND (data->>'normalized_fingerprint'='' OR data->>'normalized_fingerprint'=to_jsonb(OLD)->>'normalized_fingerprint')
  THEN RETURN NEW; END IF;
  PERFORM pg_advisory_xact_lock_shared(11042, season_key::int);
  IF current_setting('dexo.s1_source_writer',true) IS DISTINCT FROM 'S1_FINALIZED_V1'
   THEN RAISE EXCEPTION 'Bound economic policy requires compatible fenced source writer'; END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION arena_s1_policy_binding_guard() RETURNS trigger AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(11042,(SELECT season_id::int FROM arena_economicepoch WHERE id=NEW.epoch_id));
 IF NOT EXISTS (SELECT 1 FROM arena_economicepoch e WHERE e.id=NEW.epoch_id AND e.state IN ('OPEN','DRAINING'))
 THEN RAISE EXCEPTION 'Cannot bind economic policy to finalized history'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_binding_insert BEFORE INSERT ON arena_epochpolicybinding
FOR EACH ROW EXECUTE FUNCTION arena_s1_policy_binding_guard();

CREATE OR REPLACE FUNCTION arena_s1_correction_payment_guard() RETURNS trigger AS $$
DECLARE funded bigint; spent bigint; owed bigint; expected_reserve bigint;
BEGIN
 SELECT r.amount INTO funded FROM arena_categorycorrectionreserve r WHERE r.id=NEW.reserve_id FOR UPDATE;
 SELECT e.amount,cr.reserve_id INTO owed,expected_reserve FROM arena_dlxcorrectionentitlement e
 JOIN arena_dlxcorrectionround cr ON cr.id=e.round_id WHERE e.id=NEW.entitlement_id;
 SELECT COALESCE(sum(amount),0) INTO spent FROM arena_dlxcorrection WHERE reserve_id=NEW.reserve_id;
 IF funded IS NULL OR NEW.amount<>owed OR NEW.reserve_id<>expected_reserve OR spent+NEW.amount>funded
 THEN RAISE EXCEPTION 'Correction must be exact and funded by its original category reserve'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_correction_payment BEFORE INSERT ON arena_dlxcorrection
FOR EACH ROW EXECUTE FUNCTION arena_s1_correction_payment_guard();

CREATE OR REPLACE FUNCTION arena_s1_correction_adjustment_guard() RETURNS trigger AS $$
DECLARE credit bigint; held bigint; forfeited bigint;
BEGIN
 SELECT amount INTO credit FROM arena_dlxcorrection WHERE id=NEW.correction_id FOR UPDATE;
 SELECT COALESCE(sum(CASE WHEN kind='HOLD' THEN amount WHEN kind='RELEASE_HOLD' THEN -amount ELSE 0 END),0),
 COALESCE(sum(CASE WHEN kind='FORFEIT' THEN amount WHEN kind='REVERSAL' THEN -amount ELSE 0 END),0)
 INTO held,forfeited FROM arena_dlxcorrectionadjustment WHERE correction_id=NEW.correction_id;
 IF NEW.amount<=0 OR NEW.kind NOT IN ('HOLD','RELEASE_HOLD','FORFEIT','REVERSAL')
 OR (NEW.kind IN ('HOLD','FORFEIT') AND held+forfeited+NEW.amount>credit)
 OR (NEW.kind='RELEASE_HOLD' AND NEW.amount>held)
 OR (NEW.kind='REVERSAL' AND NEW.amount>forfeited)
 THEN RAISE EXCEPTION 'Correction adjustment exceeds available credit'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_correction_adjustment BEFORE INSERT ON arena_dlxcorrectionadjustment
FOR EACH ROW EXECUTE FUNCTION arena_s1_correction_adjustment_guard();

CREATE OR REPLACE FUNCTION arena_s1_creator_owner_guard() RETURNS trigger AS $$
BEGIN
 IF (OLD.creator_id,OLD.group_id,OLD.authorization_intent_id) IS DISTINCT FROM
    (NEW.creator_id,NEW.group_id,NEW.authorization_intent_id)
 THEN RAISE EXCEPTION 'Creator authorization owner is immutable'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_creator_owner BEFORE UPDATE ON network_creatorgroupregistration
FOR EACH ROW EXECUTE FUNCTION arena_s1_creator_owner_guard();

CREATE OR REPLACE FUNCTION arena_s1_creator_class_guard() RETURNS trigger AS $$
BEGIN
 IF TG_OP='DELETE' OR (to_jsonb(OLD)-'fingerprint') IS DISTINCT FROM (to_jsonb(NEW)-'fingerprint')
 OR (NEW.fingerprint IS NOT NULL AND NEW.fingerprint IS DISTINCT FROM OLD.fingerprint)
 THEN RAISE EXCEPTION 'Creator class history only permits fingerprint purge'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_creator_class BEFORE UPDATE OR DELETE ON arena_creatorcontentclass
FOR EACH ROW EXECUTE FUNCTION arena_s1_creator_class_guard();
"""
FORWARD += "\n".join(
    f"CREATE TRIGGER arena_s1_{name}_immutable BEFORE UPDATE OR DELETE ON arena_{name} "
    "FOR EACH ROW EXECUTE FUNCTION arena_reject_immutable_history();"
    for name in IMMUTABLE
)
FORWARD += "\n" + "\n".join(
    f"CREATE TRIGGER arena_s1_writer BEFORE INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION arena_s1_source_writer_guard();"
    for table in SOURCES
)
REVERSE = "\n".join(
    f"DROP TRIGGER IF EXISTS arena_s1_{name}_immutable ON arena_{name};" for name in IMMUTABLE
)
REVERSE += "\n" + "\n".join(
    f"DROP TRIGGER IF EXISTS arena_s1_writer ON {table};" for table in SOURCES
)
REVERSE += r"""
DROP TRIGGER IF EXISTS arena_s1_recognition_insert ON arena_economicrecognition;
DROP TRIGGER IF EXISTS arena_s1_binding_insert ON arena_epochpolicybinding;
DROP TRIGGER IF EXISTS arena_s1_correction_payment ON arena_dlxcorrection;
DROP TRIGGER IF EXISTS arena_s1_correction_adjustment ON arena_dlxcorrectionadjustment;
DROP TRIGGER IF EXISTS arena_s1_creator_owner ON network_creatorgroupregistration;
DROP TRIGGER IF EXISTS arena_s1_creator_class ON arena_creatorcontentclass;
DROP FUNCTION IF EXISTS arena_s1_recognition_boundary();
DROP FUNCTION IF EXISTS arena_s1_source_writer_guard();
DROP FUNCTION IF EXISTS arena_s1_policy_binding_guard();
DROP FUNCTION IF EXISTS arena_s1_correction_payment_guard();
DROP FUNCTION IF EXISTS arena_s1_correction_adjustment_guard();
DROP FUNCTION IF EXISTS arena_s1_creator_owner_guard();
DROP FUNCTION IF EXISTS arena_s1_creator_class_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("arena", "0013_phase3_finalized_economics"),
        ("network", "0007_phase3_finalized_economics"),
        ("daily", "0001_initial"),
        ("missions", "0003_b6_mission_administration"),
        ("typing_game", "0002_typingattempt_typing_run_history"),
        ("rewards", "0006_receipt_cursor_index"),
    ]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
