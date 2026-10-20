# ruff: noqa: E501
"""Owner addendum: fund a proven cutoff's mutable Epoch and later mutable periods."""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION arena_phase4_funding() RETURNS trigger AS $$
DECLARE event arena_growthunlockevent; target arena_economicepoch; origin arena_economicepoch; spent numeric;
BEGIN
 SELECT * INTO event FROM arena_growthunlockevent WHERE id=NEW.event_id FOR UPDATE;
 SELECT * INTO origin FROM arena_economicepoch WHERE id=(event.evidence->>'recognition_epoch_id')::bigint;
 SELECT * INTO target FROM arena_economicepoch WHERE id=NEW.epoch_id FOR UPDATE;
 SELECT COALESCE(sum(amount),0) INTO spent FROM arena_growthepochfunding WHERE event_id=NEW.event_id;
 IF current_setting('dexo.phase4_growth',true) IS DISTINCT FROM 'CONFIGURABLE_WEEKLY_V1'
 OR event.evidence->>'rule' IS DISTINCT FROM 'SEASON_TO_DATE_PRE_FREEZE_EQUAL_V1'
 OR origin.id IS NULL OR target.season_id<>event.season_id OR origin.season_id<>event.season_id
 OR target.state NOT IN ('OPEN','DRAINING')
 OR NOT ((target.id=origin.id AND target.state='DRAINING') OR
 (target.epoch_number>origin.epoch_number AND target.starts_at>=origin.ends_at))
 OR (event.evidence->>'qualifying_cutoff')::timestamptz<>origin.ends_at
 OR EXISTS(SELECT 1 FROM arena_epochcategorybudget WHERE epoch_id=target.id)
 OR EXISTS(SELECT 1 FROM arena_epochperformancesnapshot WHERE epoch_id=target.id)
 OR NEW.amount IS DISTINCT FROM (event.evidence->'placement'->>NEW.epoch_id::text)::bigint
 OR spent+NEW.amount>event.unlocked_amount
 THEN RAISE EXCEPTION 'Growth requires exact proven pre-Freeze mutable placement'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
"""
# Old implementation is preserved exactly for disposable reversal tests, not live downgrade.
REVERSE = r"""
CREATE OR REPLACE FUNCTION arena_phase4_funding() RETURNS trigger AS $$
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
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0024_exact_integer_policy_arithmetic")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
