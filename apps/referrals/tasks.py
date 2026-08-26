from celery import shared_task
from django.utils import timezone

from apps.referrals.models import ReferralRelation
from apps.referrals.services import activate_relation


@shared_task  # type: ignore[untyped-decorator]
def reconcile_pending_referrals(limit: int = 200) -> int:
    ids = list(
        ReferralRelation.objects.filter(status=ReferralRelation.Status.PENDING)
        .filter(next_evaluation_at__lte=timezone.now())
        .order_by("next_evaluation_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    return sum(activate_relation(item) for item in ids)
