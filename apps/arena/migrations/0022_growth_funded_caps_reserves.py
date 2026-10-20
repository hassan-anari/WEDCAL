# ruff: noqa: E501
"""Use all fresh funding in caps/reserves; numeric intermediates prevent bigint overflow."""

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
   AND NEW.amount=floor((b.base_epoch_budget::numeric+b.growth_contribution)*(p.payload->>'correction_reserve_bps')::numeric
       /(p.payload->>'scale')::numeric)
 ) THEN RAISE EXCEPTION 'Reserve must match bound fresh category funding'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE OR REPLACE FUNCTION arena_s1_entitlement_provenance() RETURNS trigger AS $$
DECLARE allowed bigint; original bigint; approved bigint; beneficiary bigint; category_key text;
BEGIN
 SELECT d.player_id,d.category INTO beneficiary,category_key
 FROM arena_economicdeferral d WHERE d.id=NEW.deferral_id;
 SELECT floor((b.base_epoch_budget::numeric+b.growth_contribution)*(p.payload->>'player_cap_bps')::numeric
        /(p.payload->>'scale')::numeric) INTO allowed
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
CREATE OR REPLACE FUNCTION arena_s1_allocation_writer_guard() RETURNS trigger AS $$
DECLARE season_key bigint; cap_amount bigint; minimum_score bigint;
BEGIN
 SELECT season_id INTO season_key FROM arena_economicepoch WHERE id=NEW.epoch_id;
 IF EXISTS (SELECT 1 FROM arena_epochpolicybinding b JOIN arena_economicepoch e
 ON e.id=b.epoch_id WHERE e.season_id=season_key) THEN
  PERFORM pg_advisory_xact_lock_shared(11042, season_key::int);
  IF current_setting('dexo.s1_source_writer',true) IS DISTINCT FROM 'S1_FINALIZED_V1'
   THEN RAISE EXCEPTION 'Bound allocations require compatible fenced writer'; END IF;
  SELECT floor((budget.base_epoch_budget::numeric+budget.growth_contribution)*(p.payload->>'player_cap_bps')::numeric
   /(p.payload->>'scale')::numeric), (p.payload->>'minimum_category_score')::bigint
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
"""

REVERSE = r"""
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
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0021_configurable_economic_guards")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
