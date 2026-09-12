from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION rewards_rewardledger_reject_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'RewardLedger is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER rewards_rewardledger_append_only
BEFORE UPDATE OR DELETE ON rewards_rewardledger
FOR EACH ROW EXECUTE FUNCTION rewards_rewardledger_reject_mutation();
"""

REVERSE = """
DROP TRIGGER IF EXISTS rewards_rewardledger_append_only ON rewards_rewardledger;
DROP FUNCTION IF EXISTS rewards_rewardledger_reject_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("rewards", "0004_rewardsettlementoutbox_claimed_at_and_more")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
