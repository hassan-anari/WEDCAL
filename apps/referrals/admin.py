from django.contrib import admin
from django.http import HttpRequest

from apps.referrals.models import ReferralCode, ReferralRelation, ReferralShareOutcome


class ImmutableAuditAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False


@admin.register(ReferralCode)
class ReferralCodeAdmin(ImmutableAuditAdmin):
    list_display = ("id", "player", "code", "created_at")


@admin.register(ReferralRelation)
class ReferralRelationAdmin(ImmutableAuditAdmin):
    list_display = ("id", "inviter", "invitee", "status", "attributed_at", "verified_at")


@admin.register(ReferralShareOutcome)
class ReferralShareOutcomeAdmin(ImmutableAuditAdmin):
    list_display = ("id", "source_reward", "tier_key", "share_bps", "amount", "created_at")
