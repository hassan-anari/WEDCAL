import structlog
from celery import shared_task
from django.utils import timezone

from apps.referrals.models import ReferralRelation
from apps.referrals.services import activate_relation

logger = structlog.get_logger(__name__)


@shared_task  # type: ignore[untyped-decorator]
def reconcile_pending_referrals(limit: int = 200) -> int:
    ids = list(
        ReferralRelation.objects.filter(status=ReferralRelation.Status.PENDING)
        .filter(next_evaluation_at__lte=timezone.now())
        .order_by("next_evaluation_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    activated = 0
    for relation_id in ids:
        try:
            activated += activate_relation(relation_id)
        except Exception as exc:
            logger.warning(
                "referral_reconciliation_failed",
                relation_id=relation_id,
                error_category=type(exc).__name__,
            )
    return activated
