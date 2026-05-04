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
    path(
        "admin/requests/<int:pk>/reject/",
        admin_views.admin_reject_service_request,
        name="admin_reject_service_request",
    ),
    path("admin/requests/<int:pk>/assign-inspector/", admin_views.assign_inspector, name="assign_inspector"),
    path("admin/requests/<int:pk>/waive-inspection/", admin_views.waive_inspection, name="waive_inspection"),
    path("admin/requests/<int:pk>/proceed-to-computation/", admin_views.proceed_to_computation, name="proceed_to_computation"),
    path(
        "admin/requests/<int:pk>/assign-waived-crew/",
        admin_views.assign_waived_inspection_crew,
        name="assign_waived_inspection_crew",
    ),
    path("admin/requests/<int:pk>/schedule-desludging/", admin_views.schedule_desludging, name="schedule_desludging"),
    path("admin/requests/<int:pk>/confirm-payment/", admin_views.confirm_payment, name="confirm_payment"),
    path(
        "admin/requests/<int:pk>/confirm-grass/",
        admin_views.confirm_grass_request,
        name="confirm_grass_request",
    ),
    path(
        "admin/requests/<int:pk>/cancel-grass/",
        admin_views.cancel_grass_request,
        name="cancel_grass_request",
    ),
    path("admin/requests/<int:pk>/confirm-inspection-fee/", admin_views.confirm_inspection_fee, name="confirm_inspection_fee"),
    path("admin/requests/<int:pk>/reject-inspection-fee/", admin_views.reject_inspection_fee, name="reject_inspection_fee"),
    path(
        "admin/requests/<int:pk>/waive-public-bayawan-inspection-fee/",
        admin_views.waive_public_bayawan_inspection_fee,
        name="waive_public_bayawan_inspection_fee",
    ),
    path(
        "admin/requests/<int:pk>/waive-bawad-inspection-fee/",
        admin_views.waive_bawad_inspection_fee_after_proof,
        name="waive_bawad_inspection_fee",
    ),
    path("admin/requests/schedule/", admin_views.admin_schedule_by_barangay, name="admin_schedule"),
    path("admin/membership/", admin_views.admin_membership, name="admin_membership"),
    path(
        "admin/contact-change-requests/",
        admin_views.admin_profile_contact_requests,
        name="admin_profile_contact_requests",
    ),
    path(
        "admin/contact-change-requests/<int:pk>/approve/",
        admin_views.admin_profile_contact_approve,
        name="admin_profile_contact_approve",
    ),
    path(
        "admin/contact-change-requests/<int:pk>/reject/",
        admin_views.admin_profile_contact_reject,
        name="admin_profile_contact_reject",
    ),
    path("admin/equipment/", admin_views.admin_equipment, name="admin_equipment"),
    path("admin/membership/update-volume/<int:user_id>/", admin_views.admin_update_prior_volume, name="admin_update_prior_volume"),
    path("admin/membership/history/<int:user_id>/", admin_views.member_service_history, name="member_service_history"),
    path("admin/computation/", admin_views.admin_computation, name="admin_computation"),
    path("admin/computation/generate-receipt/", admin_views.generate_receipt, name="generate_receipt"),
    path("admin/declogging-app/", admin_views.admin_declogging_app, name="admin_declogging_app"),
    path("admin/map-requests/", admin_views.admin_map_requests, name="admin_map_requests"),
    path("admin/analytics/", admin_views.admin_analytics, name="admin_analytics"),
    path("admin/analytics/api/", admin_views.analytics_api, name="admin_analytics_api"),
]
