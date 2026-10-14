from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("rewards", "0005_rewardledger_append_only")]
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS reward_player_receipt_cursor "
                    "ON rewards_rewardledger (player_id, id DESC)",
                    "DROP INDEX CONCURRENTLY IF EXISTS reward_player_receipt_cursor",
                ),
                migrations.RunSQL(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_index WHERE "
                    "indexrelid = 'reward_player_receipt_cursor'::regclass AND indisvalid) "
                    "THEN RAISE EXCEPTION 'Invalid cursor index: drop concurrently and retry'; "
                    "END IF; END $$",
                    migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="rewardledger",
                    index=models.Index(
                        fields=("player", "-id"), name="reward_player_receipt_cursor"
                    ),
                )
            ],
        )
    ]
