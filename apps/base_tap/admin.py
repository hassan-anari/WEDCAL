from django.contrib import admin

from apps.base_tap.models import BaseTapBatch, BaseTapDailyAccumulator

admin.site.register(BaseTapBatch)
admin.site.register(BaseTapDailyAccumulator)
