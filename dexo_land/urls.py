from django.contrib import admin
from django.urls import path

from apps.core.views import live, metrics, ready
from dexo_land.api import api

admin.site.site_header = "Dexo Land Administration"
admin.site.site_title = "Dexo Land Admin"
admin.site.index_title = "Administration"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("health/live", live, name="health-live"),
    path("health/ready", ready, name="health-ready"),
    path("operations/metrics", metrics, name="operations-metrics"),
]
