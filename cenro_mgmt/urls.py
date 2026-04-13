import re

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView, TemplateView
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include(("accounts.urls", "accounts"), namespace="accounts")),
    path("services/", include(("services.urls", "services"), namespace="services")),
    path("scheduling/", include(("scheduling.urls", "scheduling"), namespace="scheduling")),
    path("dashboard/", include(("dashboard.urls", "dashboard"), namespace="dashboard")),
    path("offline/", TemplateView.as_view(template_name="offline.html"), name="offline"),
    path("sw.js", TemplateView.as_view(template_name="sw.js", content_type="application/javascript"), name="sw"),
    path("", RedirectView.as_view(url="/dashboard/", permanent=False)),
]

# django.conf.urls.static.static() refuses to add routes when DEBUG=False; use SERVE_MEDIA instead.
if getattr(settings, "SERVE_MEDIA", False) and settings.MEDIA_URL and settings.MEDIA_ROOT:
    _media_prefix = settings.MEDIA_URL.lstrip("/")
    if _media_prefix:
        urlpatterns += [
            re_path(
                r"^%s(?P<path>.*)$" % re.escape(_media_prefix),
                serve,
                {"document_root": str(settings.MEDIA_ROOT)},
            ),
        ]

