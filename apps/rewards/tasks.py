from celery import shared_task
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.rewards.models import RewardSettlementOutbox


def consume_outbox(outbox_id: int) -> bool:
    with transaction.atomic():
        item = (
            RewardSettlementOutbox.objects.select_for_update()
            .select_related("reward")
            .get(pk=outbox_id)
        )
        if item.state == RewardSettlementOutbox.State.PROCESSED:
            return False
        item.state = RewardSettlementOutbox.State.PROCESSING
        item.attempts = F("attempts") + 1
        item.save(update_fields=("state", "attempts", "updated_at"))
    try:
        from apps.referrals.models import ReferralRelation
        from apps.referrals.services import activate_relation, process_source_reward

        relation = ReferralRelation.objects.filter(
            invitee_id=item.reward.player_id, status=ReferralRelation.Status.PENDING
        ).first()
        if relation:
            activate_relation(relation.pk, at=timezone.now())
        process_source_reward(item.reward)
    except Exception as exc:
        RewardSettlementOutbox.objects.filter(pk=outbox_id).update(
            state=RewardSettlementOutbox.State.PENDING, last_error_category=type(exc).__name__[:100]
        )
        raise
    RewardSettlementOutbox.objects.filter(pk=outbox_id).update(
        state=RewardSettlementOutbox.State.PROCESSED,
        processed_at=timezone.now(),
        last_error_category="",
    )
    return True


@shared_task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)  # type: ignore[untyped-decorator]
def process_reward_outbox(outbox_id: int) -> bool:
    return consume_outbox(outbox_id)


@shared_task  # type: ignore[untyped-decorator]
def recover_reward_outbox(limit: int = 200) -> int:
    ids = list(
        RewardSettlementOutbox.objects.filter(
            state=RewardSettlementOutbox.State.PENDING, available_at__lte=timezone.now()
        )
        .order_by("id")
        .values_list("id", flat=True)[:limit]
    )
    for item in ids:
        process_reward_outbox.delay(item)
    return len(ids)
