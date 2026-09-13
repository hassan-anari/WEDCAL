from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION telegram_dead_letter_disposition_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'Telegram dead-letter disposition is immutable'; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER telegram_dead_letter_disposition_immutable_trigger BEFORE UPDATE OR DELETE
ON telegram_telegramdeadletterdisposition FOR EACH ROW
EXECUTE FUNCTION telegram_dead_letter_disposition_immutable();
"""
REVERSE = r"""
DROP TRIGGER IF EXISTS telegram_dead_letter_disposition_immutable_trigger
ON telegram_telegramdeadletterdisposition;
DROP FUNCTION IF EXISTS telegram_dead_letter_disposition_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("telegram", "0006_telegramdeadletterdisposition")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
