from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION arena_reject_ledger_mutation()
RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'DLX ledgers are append-only'; END; $$ LANGUAGE plpgsql;
CREATE TRIGGER arena_dlxallocation_append_only BEFORE UPDATE OR DELETE ON arena_dlxallocation
FOR EACH ROW EXECUTE FUNCTION arena_reject_ledger_mutation();
CREATE TRIGGER arena_dlxadjustment_append_only BEFORE UPDATE OR DELETE ON arena_dlxadjustment
FOR EACH ROW EXECUTE FUNCTION arena_reject_ledger_mutation();
"""
REVERSE = """
DROP TRIGGER IF EXISTS arena_dlxallocation_append_only ON arena_dlxallocation;
DROP TRIGGER IF EXISTS arena_dlxadjustment_append_only ON arena_dlxadjustment;
DROP FUNCTION IF EXISTS arena_reject_ledger_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
