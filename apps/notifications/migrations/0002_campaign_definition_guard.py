from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION notification_campaign_definition_guard() RETURNS trigger AS $$
BEGIN
  IF (OLD.state <> 'DRAFT' OR NEW.state <> 'DRAFT') AND (
    NEW.audience_type IS DISTINCT FROM OLD.audience_type OR
    NEW.title_snapshot IS DISTINCT FROM OLD.title_snapshot OR
    NEW.body_snapshot IS DISTINCT FROM OLD.body_snapshot OR
    NEW.purpose IS DISTINCT FROM OLD.purpose
  ) THEN
    RAISE EXCEPTION 'Campaign content and audience are immutable after draft';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER notification_campaign_definition_guard_trigger
BEFORE UPDATE ON notifications_broadcastcampaign
FOR EACH ROW EXECUTE FUNCTION notification_campaign_definition_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS notification_campaign_definition_guard_trigger
ON notifications_broadcastcampaign;
DROP FUNCTION IF EXISTS notification_campaign_definition_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("notifications", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
