from django.db import migrations

FORWARD = r"""
CREATE TRIGGER arena_anomaly_resolution_immutable BEFORE UPDATE OR DELETE
ON arena_epochanomalyresolution FOR EACH ROW
EXECUTE FUNCTION arena_residual_immutable();
"""
REVERSE = r"""
DROP TRIGGER IF EXISTS arena_anomaly_resolution_immutable ON arena_epochanomalyresolution;
"""


class Migration(migrations.Migration):
    dependencies = [("arena", "0009_epochanomalyresolution")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
