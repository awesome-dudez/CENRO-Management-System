from django.urls import path

from . import views
from . import admin_views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    # Admin routes
    path("admin/", admin_views.admin_dashboard, name="admin_dashboard"),
    path("admin/requests/", admin_views.admin_requests, name="admin_requests"),
    path("admin/requests/approve/<int:pk>/", admin_views.approve_request, name="approve_request"),
    path("admin/requests/<int:pk>/assign-inspector/", admin_views.assign_inspector, name="assign_inspector"),
    path("admin/requests/<int:pk>/schedule-desludging/", admin_views.schedule_desludging, name="schedule_desludging"),
    path("admin/requests/<int:pk>/confirm-payment/", admin_views.confirm_payment, name="confirm_payment"),
    path("admin/requests/schedule/", admin_views.admin_schedule_by_barangay, name="admin_schedule"),
    path("admin/membership/", admin_views.admin_membership, name="admin_membership"),
    path("admin/membership/history/<int:user_id>/", admin_views.member_service_history, name="member_service_history"),
    path("admin/computation/", admin_views.admin_computation, name="admin_computation"),
    path("admin/computation/generate-receipt/", admin_views.generate_receipt, name="generate_receipt"),
    path("admin/declogging-app/", admin_views.admin_declogging_app, name="admin_declogging_app"),
    path("admin/map-requests/", admin_views.admin_map_requests, name="admin_map_requests"),
    path("admin/analytics/", admin_views.admin_analytics, name="admin_analytics"),
    path("admin/analytics/api/", admin_views.analytics_api, name="admin_analytics_api"),
]
