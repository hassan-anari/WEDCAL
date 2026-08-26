from django.contrib import admin

from apps.referrals.models import ReferralCode, ReferralRelation, ReferralShareOutcome

admin.site.register((ReferralCode, ReferralRelation, ReferralShareOutcome))
