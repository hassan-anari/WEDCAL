from celery import shared_task

from apps.referrals.models import ReferralRelation
from apps.referrals.services import activate_relation


@shared_task  # type: ignore[untyped-decorator]
def reconcile_pending_referrals(limit: int = 200) -> int:
    ids = list(
        ReferralRelation.objects.filter(status=ReferralRelation.Status.PENDING)
        .order_by("id")
        .values_list("id", flat=True)[:limit]
    )
    return sum(activate_relation(item) for item in ids)
