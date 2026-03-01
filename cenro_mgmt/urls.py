from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include(("accounts.urls", "accounts"), namespace="accounts")),
    path("services/", include(("services.urls", "services"), namespace="services")),
    path("scheduling/", include(("scheduling.urls", "scheduling"), namespace="scheduling")),
    path("dashboard/", include(("dashboard.urls", "dashboard"), namespace="dashboard")),
    path("", RedirectView.as_view(url="/dashboard/", permanent=False)),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

