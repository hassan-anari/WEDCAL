from django.db import migrations

FORWARD = """
CREATE TRIGGER arena_performance_source_immutable
BEFORE UPDATE OR DELETE ON arena_performancesourcefact
FOR EACH ROW EXECUTE FUNCTION arena_reject_immutable_history();
"""
REVERSE = """
DROP TRIGGER IF EXISTS arena_performance_source_immutable ON arena_performancesourcefact;
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0005_performancesourcefact")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
