from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.clans.economy import settle_clan_reward
from apps.clans.models import ClanActivity, ClanActivityWindow
from apps.game_config.services import resolve_game_config_version


def finalize_due_windows(*, limit: int = 100) -> int:
    now = timezone.now()
    processed = 0
    rows = (
        ClanActivity.objects.filter(
            window_processed=False, rewardable_chars__gt=0, observed_at__lt=now
        )
        .select_related("membership", "clan_season", "season", "config_version")
        .order_by("observed_at", "pk")[:limit]
    )
    for activity in rows:
        config = resolve_game_config_version(activity.config_version_id)
        policy = config.clan
        if policy is None or activity.membership is None:
            continue
        epoch = int(activity.observed_at.timestamp())
        start_epoch = epoch - epoch % policy.activity.window_seconds
        start = activity.observed_at.fromtimestamp(start_epoch, tz=activity.observed_at.tzinfo)
        with transaction.atomic():
            window, created = ClanActivityWindow.objects.get_or_create(
                membership=activity.membership,
                season=activity.season,
                config_version=activity.config_version,
                window_start=start,
                defaults={"clan_season": activity.clan_season},
            )
            if window.finalized_at:
                activity.window_processed = True
                activity.save(update_fields=("window_processed",))
                continue
            source = ClanActivity.objects.filter(
                membership=activity.membership,
                config_version=activity.config_version,
                observed_at__gte=start,
                observed_at__lt=start + timedelta(seconds=policy.activity.window_seconds),
                classification__in=[
                    ClanActivity.Classification.VALID,
                    ClanActivity.Classification.SUSPICIOUS,
                ],
            )
            chars = source.aggregate(total=Sum("rewardable_chars"))["total"] or 0
            gross = min(
                chars // policy.activity.chars_per_gross_point,
                policy.activity.max_gross_points_per_window,
            )
            window.eligible_chars = chars
            window.gross_amount = gross
            window.finalized_at = now
            if gross:
                settlement, _ = settle_clan_reward(
                    membership=activity.membership,
                    source_type="CLAN_ACTIVITY",
                    source_id=f"window:{window.pk}",
                    gross_amount=gross,
                    idempotency_key=f"clan-window:{window.pk}",
                    occurred_at=start,
                    resolved_config=config,
                    metadata={"window_start": start.isoformat()},
                )
                window.settlement = settlement
            window.save()
            source.update(window_processed=True)
            processed += 1
    return processed
