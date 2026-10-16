# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.notifications.models import (
    BroadcastCampaign,
    BroadcastRecipient,
    GroupBroadcastSubscription,
)
from apps.notifications.services import materialize_campaign
from apps.notifications.tests.test_services import authorized_operator

pytestmark = pytest.mark.django_db(transaction=True)


def test_upgrade_preserves_legacy_targets_preferences_and_confirmation_scope():
    executor = MigrationExecutor(connection)
    previous = ("notifications", "0007_notification_notification_feed_page_and_more")
    current = ("notifications", "0008_owner_broadcast_audience")
    actor = authorized_operator("migration-operator")
    try:
        executor.migrate([previous])
        state = executor.loader.project_state([previous]).apps
        Group = state.get_model("telegram", "TelegramGroup")
        Subscription = state.get_model("notifications", "GroupBroadcastSubscription")
        Campaign = state.get_model("notifications", "BroadcastCampaign")
        Recipient = state.get_model("notifications", "BroadcastRecipient")
        now = timezone.now()
        group = Group.objects.create(
            telegram_chat_id=-900,
            chat_type="supergroup",
            bot_is_admin=True,
            bot_membership_status="administrator",
            first_seen_at=now,
            last_seen_at=now,
        )
        subscription = Subscription.objects.create(
            group_id=group.pk,
            enabled=False,
            revoked_at=now,
            authorized_telegram_user_id=123,
            revision=7,
        )
        campaign = Campaign.objects.create(
            created_by_id=actor.pk,
            audience_type="GROUPS",
            state="MATERIALIZING",
            body_snapshot="Synthetic legacy confirmation",
            purpose="Migration test",
            group_materialization_cursor=subscription.pk,
        )
        target = Recipient.objects.create(
            campaign_id=campaign.pk,
            group_subscription_id=subscription.pk,
            target_kind="GROUP",
            telegram_user_id=-900,
            subscription_revision=7,
        )
        original = Subscription.objects.filter(pk=subscription.pk).values().get()
        executor = MigrationExecutor(connection)
        executor.migrate([current])
        # B5 adds source columns; restore current leaves before using runtime models.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        legacy = BroadcastCampaign.objects.get(pk=campaign.pk)
        assert legacy.audience_policy_version == 1
        assert (
            materialize_campaign(legacy.pk) == 0
        )  # No unconfirmed audience expansion/cursor reinterpretation.
        retained = BroadcastRecipient.objects.get(pk=target.pk)
        assert retained.group_id is None and retained.group_subscription_id == subscription.pk
        assert retained.subscription_revision == 7 and retained.state == "PENDING"
        assert (
            GroupBroadcastSubscription.objects.filter(pk=subscription.pk).values().get() == original
        )
        assert (
            BroadcastCampaign.objects.create(
                created_by=actor,
                audience_type="GROUPS",
                body_snapshot="New",
                purpose="Test",
            ).audience_policy_version
            == 2
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
