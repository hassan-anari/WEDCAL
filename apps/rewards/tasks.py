from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.rewards.models import RewardSettlementOutbox


def consume_outbox(outbox_id: int) -> bool:
    season_id = RewardSettlementOutbox.objects.values_list("reward__season_id", flat=True).get(
        pk=outbox_id
    )
    from apps.seasons.barrier import economic_write_session_barrier

    with economic_write_session_barrier(season_id):
        return _consume_outbox(outbox_id)


def _consume_outbox(outbox_id: int) -> bool:
    now = timezone.now()
    lease_until = now + timedelta(seconds=settings.REWARD_OUTBOX_LEASE_SECONDS)

    with transaction.atomic():
        item = (
            RewardSettlementOutbox.objects.select_for_update()
            .select_related("reward__season")
            .get(pk=outbox_id)
        )
        if item.state in {
            RewardSettlementOutbox.State.PROCESSED,
            RewardSettlementOutbox.State.DEFERRED,
        }:
            return False
        if (
            item.state == RewardSettlementOutbox.State.PROCESSING
            and item.lease_until
            and item.lease_until > now
        ):
            return False
        item.state = RewardSettlementOutbox.State.PROCESSING
        item.claimed_at = now
        item.lease_until = lease_until
        item.attempts = F("attempts") + 1
        item.save(update_fields=("state", "claimed_at", "lease_until", "attempts", "updated_at"))
    try:
        from apps.referrals.models import ReferralRelation
        from apps.referrals.services import (
            DeferredReferralSettlement,
            activate_relation,
            process_source_reward,
        )

        relation = ReferralRelation.objects.filter(
            invitee_id=item.reward.player_id,
            status=ReferralRelation.Status.PENDING,
            next_evaluation_at__lte=now,
        ).first()
        if relation:
            activate_relation(relation.pk, at=now)
        process_source_reward(item.reward)
    except DeferredReferralSettlement:
        RewardSettlementOutbox.objects.filter(pk=outbox_id).update(
            state=RewardSettlementOutbox.State.DEFERRED,
            lease_until=None,
            last_error_category="season_closed",
        )
        return False
    except Exception as exc:
        RewardSettlementOutbox.objects.filter(pk=outbox_id).update(
            state=RewardSettlementOutbox.State.PENDING,
            lease_until=None,
            next_attempt_at=now + timedelta(seconds=settings.REWARD_OUTBOX_RETRY_SECONDS),
            last_error_category=type(exc).__name__[:100],
        )
        raise
    RewardSettlementOutbox.objects.filter(pk=outbox_id).update(
        state=RewardSettlementOutbox.State.PROCESSED,
        processed_at=timezone.now(),
        lease_until=None,
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
    now = timezone.now()
    ids = list(
        RewardSettlementOutbox.objects.filter(
            Q(state=RewardSettlementOutbox.State.PENDING, next_attempt_at__lte=now)
            | Q(state=RewardSettlementOutbox.State.PROCESSING, lease_until__lte=now)
        )
        .order_by("next_attempt_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    for item in ids:
        process_reward_outbox.delay(item)
    return len(ids)
