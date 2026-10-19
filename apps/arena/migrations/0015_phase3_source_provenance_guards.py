from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION arena_s1_reserve_provenance() RETURNS trigger AS $$
BEGIN
 IF NOT EXISTS (
  SELECT 1 FROM arena_epochcategorybudget b
  JOIN arena_economicepoch e ON e.id=b.epoch_id
  JOIN arena_epochpolicybinding binding ON binding.epoch_id=e.id
  JOIN arena_economicpolicyversion p ON p.id=binding.policy_id
  WHERE b.id=NEW.budget_id AND p.id=NEW.policy_id AND e.state='DRAINING'
   AND NEW.amount=b.base_epoch_budget*(p.payload->>'correction_reserve_bps')::bigint
       /(p.payload->>'scale')::bigint
 ) THEN RAISE EXCEPTION 'Reserve must match bound fresh category funding'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_reserve_insert BEFORE INSERT ON arena_categorycorrectionreserve
FOR EACH ROW EXECUTE FUNCTION arena_s1_reserve_provenance();

CREATE OR REPLACE FUNCTION arena_s1_entitlement_provenance() RETURNS trigger AS $$
DECLARE allowed bigint; original bigint; approved bigint; beneficiary bigint; category_key text;
BEGIN
 SELECT d.player_id,d.category INTO beneficiary,category_key
 FROM arena_economicdeferral d WHERE d.id=NEW.deferral_id;
 SELECT b.base_epoch_budget*(p.payload->>'player_cap_bps')::bigint
        /(p.payload->>'scale')::bigint INTO allowed
 FROM arena_dlxcorrectionround cr
 JOIN arena_categorycorrectionreserve r ON r.id=cr.reserve_id
 JOIN arena_epochcategorybudget b ON b.id=r.budget_id
 JOIN arena_economicpolicyversion p ON p.id=r.policy_id
 JOIN arena_economicdeferral d ON d.epoch_id=b.epoch_id AND d.id=NEW.deferral_id
 WHERE cr.id=NEW.round_id AND b.category=NEW.category;
 IF allowed IS NULL OR beneficiary<>NEW.player_id OR category_key<>NEW.category
 THEN RAISE EXCEPTION 'Correction beneficiary/category/Epoch provenance mismatch'; END IF;
 PERFORM 1 FROM arena_dlxcorrectionround WHERE id=NEW.round_id FOR UPDATE;
 SELECT COALESCE(sum(amount),0) INTO original FROM arena_dlxallocation
 WHERE player_id=NEW.player_id AND category=NEW.category
  AND epoch_id=(SELECT epoch_id FROM arena_economicdeferral WHERE id=NEW.deferral_id);
 SELECT COALESCE(sum(e.amount),0) INTO approved FROM arena_dlxcorrectionentitlement e
 JOIN arena_economicdeferral d ON d.id=e.deferral_id
 WHERE e.player_id=NEW.player_id AND e.category=NEW.category
 AND d.epoch_id=(SELECT epoch_id FROM arena_economicdeferral WHERE id=NEW.deferral_id);
 IF NEW.amount<=0 OR original+approved+NEW.amount>allowed
 THEN RAISE EXCEPTION 'Positive correction exceeds original player cap'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_entitlement_insert BEFORE INSERT ON arena_dlxcorrectionentitlement
FOR EACH ROW EXECUTE FUNCTION arena_s1_entitlement_provenance();

CREATE OR REPLACE FUNCTION arena_s1_canonical_history() RETURNS trigger AS $$
BEGIN
 IF TG_TABLE_NAME='clans_clancontributionentry' THEN
  RAISE EXCEPTION 'Canonical Clan Contribution is append-only';
 ELSIF TG_TABLE_NAME='network_creatorregistrationstateevent' THEN
  RAISE EXCEPTION 'Historical Creator authorization is append-only';
 ELSIF TG_TABLE_NAME='players_player' THEN
  IF OLD.first_authenticated_at IS NOT NULL AND
     OLD.first_authenticated_at IS DISTINCT FROM NEW.first_authenticated_at
  THEN RAISE EXCEPTION 'Trusted first authentication cannot be rewritten'; END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_contribution_immutable BEFORE UPDATE OR DELETE
ON clans_clancontributionentry FOR EACH ROW EXECUTE FUNCTION arena_s1_canonical_history();
CREATE TRIGGER arena_s1_registration_history_immutable BEFORE UPDATE OR DELETE
ON network_creatorregistrationstateevent FOR EACH ROW EXECUTE FUNCTION arena_s1_canonical_history();
CREATE TRIGGER arena_s1_trusted_age_immutable BEFORE UPDATE
ON players_player FOR EACH ROW EXECUTE FUNCTION arena_s1_canonical_history();

CREATE OR REPLACE FUNCTION arena_s1_verified_referral_history() RETURNS trigger AS $$
BEGIN
 IF (OLD.inviter_id,OLD.invitee_id,OLD.attributed_at) IS DISTINCT FROM
    (NEW.inviter_id,NEW.invitee_id,NEW.attributed_at) OR
 (OLD.status='VERIFIED' AND (OLD.status,OLD.verified_at,OLD.verified_config_id)
   IS DISTINCT FROM (NEW.status,NEW.verified_at,NEW.verified_config_id))
 THEN RAISE EXCEPTION 'Referral attribution and actual verification are immutable'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_s1_referral_history BEFORE UPDATE ON referrals_referralrelation
FOR EACH ROW EXECUTE FUNCTION arena_s1_verified_referral_history();
"""
REVERSE = r"""
DROP TRIGGER IF EXISTS arena_s1_reserve_insert ON arena_categorycorrectionreserve;
DROP TRIGGER IF EXISTS arena_s1_entitlement_insert ON arena_dlxcorrectionentitlement;
DROP TRIGGER IF EXISTS arena_s1_contribution_immutable ON clans_clancontributionentry;
DROP TRIGGER IF EXISTS arena_s1_registration_history_immutable
 ON network_creatorregistrationstateevent;
DROP TRIGGER IF EXISTS arena_s1_trusted_age_immutable ON players_player;
DROP TRIGGER IF EXISTS arena_s1_referral_history ON referrals_referralrelation;
DROP FUNCTION IF EXISTS arena_s1_reserve_provenance();
DROP FUNCTION IF EXISTS arena_s1_entitlement_provenance();
DROP FUNCTION IF EXISTS arena_s1_canonical_history();
DROP FUNCTION IF EXISTS arena_s1_verified_referral_history();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("arena", "0014_phase3_economic_guards"),
        ("referrals", "0002_referralrelation_next_evaluation_at"),
    ]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
