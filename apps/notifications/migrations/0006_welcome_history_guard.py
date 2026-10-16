from django.db import migrations

SQL = """
CREATE FUNCTION dexo_welcome_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Welcome versions are immutable; create a new version';
END;
$$;
CREATE TRIGGER welcome_immutable BEFORE UPDATE OR DELETE ON notifications_welcometemplate
FOR EACH ROW EXECUTE FUNCTION dexo_welcome_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("notifications", "0005_welcometemplate_welcomeconfiguration")]
    operations = [
        migrations.RunSQL(
            SQL,
            "DROP TRIGGER welcome_immutable ON notifications_welcometemplate; "
            "DROP FUNCTION dexo_welcome_immutable();",
        )
    ]
