"""Low-cardinality operational snapshots; metrics are never correctness state."""

from typing import Any

from django.db.models import Count, Min
from django.utils import timezone


def outbox_metrics() -> dict[str, Any]:
    from apps.rewards.models import RewardSettlementOutbox

    rows = {
        row["state"]: row["count"]
        for row in RewardSettlementOutbox.objects.values("state").annotate(count=Count("id"))
    }
    oldest = RewardSettlementOutbox.objects.exclude(state="PROCESSED").aggregate(
        value=Min("available_at")
    )["value"]
    return {
        "pending": rows.get("PENDING", 0),
        "processing": rows.get("PROCESSING", 0),
        "deferred": rows.get("DEFERRED", 0),
        "oldest_eligible_age_seconds": (
            max(int((timezone.now() - oldest).total_seconds()), 0) if oldest else 0
        ),
        "retries": RewardSettlementOutbox.objects.filter(attempts__gt=1).count(),
        "failures_by_category": list(
            RewardSettlementOutbox.objects.exclude(last_error_category="")
            .values("last_error_category")
            .annotate(count=Count("id"))
            .order_by("last_error_category")
        ),
    }


def operational_snapshot() -> dict[str, Any]:
    from apps.arena.models import EconomicAutomationAttempt, RankingProjectionEvent
    from apps.base_tap.models import BaseTapDailyAccumulator
    from apps.network.models import CreatorWindowSettlement
    from apps.notifications.models import BroadcastRecipient, TelegramDelivery
    from apps.referrals.models import ReferralRelation
    from apps.seasons.models import SeasonCloseRun
    from apps.telegram.models import TelegramInboundUpdate

    return {
        "telegram_ingress_pending": TelegramInboundUpdate.objects.exclude(
            state__in=("PROCESSED", "IGNORED")
        ).count(),
        "reward_outbox": outbox_metrics(),
        "dlx_automation": list(
            EconomicAutomationAttempt.objects.values("status", "error_code")
            .annotate(count=Count("pk"))
            .order_by("status", "error_code")
        ),
        "base_tap_pending": BaseTapDailyAccumulator.objects.filter(pending_points__gt=0).count(),
        "creator_windows_pending": CreatorWindowSettlement.objects.filter(
            finalized_at__isnull=True
        ).count(),
        "referrals_pending": ReferralRelation.objects.filter(status="PENDING").count(),
        "ranking_events_pending": RankingProjectionEvent.objects.filter(
            processed_at__isnull=True
        ).count(),
        "season_close_stages": list(
            SeasonCloseRun.objects.values("stage").annotate(count=Count("id")).order_by("stage")
        ),
        "notification_pending": TelegramDelivery.objects.filter(state="PENDING").count(),
        "broadcast_pending": BroadcastRecipient.objects.filter(state="PENDING").count(),
    }
