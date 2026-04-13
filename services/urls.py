from django.urls import path

from . import views

app_name = "services"

urlpatterns = [
    path("request/create/", views.create_request, name="create_request"),
    path("request/grasscutting-application/", views.grasscutting_application, name="grasscutting_application"),
    path("api/offline/create-request/", views.offline_create_request, name="offline_create_request"),
    path("api/reverse-geocode/", views.reverse_geocode, name="reverse_geocode"),
    path("api/verify-other-consumer/", views.verify_other_consumer, name="verify_other_consumer"),
    path("request/<int:pk>/", views.request_detail, name="request_detail"),
    path("request/<int:pk>/grasscutting-detail/", views.grasscutting_request_detail, name="grasscutting_request_detail"),
    path("request/<int:pk>/grasscutting-view/", views.grasscutting_request_view, name="grasscutting_request_view"),
    path("requests/", views.request_list, name="request_list"),
    path("history/", views.history, name="history"),
    path("clients/", views.client_records, name="client_records"),
    path("request/<int:pk>/inspect/", views.submit_inspection, name="submit_inspection"),
    path("request/<int:pk>/complete-info/", views.submit_completion, name="submit_completion"),
    path("request/<int:pk>/computation/", views.view_computation, name="view_computation"),
    path("request/<int:pk>/computation/download/", views.download_computation_pdf, name="download_computation_pdf"),
    path("request/<int:pk>/computation/edit/", views.edit_computation, name="edit_computation"),
    path("request/<int:pk>/upload-receipt/", views.upload_receipt, name="upload_receipt"),
    path("request/<int:pk>/treasurer-receipt/view/", views.view_treasurer_receipt, name="view_treasurer_receipt"),
    path("request/<int:pk>/inspection-fee-bill/", views.inspection_fee_bill, name="inspection_fee_bill"),
    path("request/<int:pk>/inspection-fee-bill/download/", views.download_inspection_fee_bill_pdf, name="download_inspection_fee_bill_pdf"),
    path("request/<int:pk>/upload-inspection-fee/", views.upload_inspection_fee_receipt, name="upload_inspection_fee"),
    path("request/<int:pk>/inspection-fee-receipt/view/", views.view_inspection_fee_receipt, name="view_inspection_fee_receipt"),
    path("request/<int:pk>/print/", views.print_application, name="print_application"),
    path("request/<int:pk>/complete/", views.complete_request, name="complete_request"),
    path("notifications/", views.notification_list, name="notification_list"),
    path("notifications/mark-read/<int:pk>/", views.mark_notification_read, name="mark_notification_read"),
    path("api/notifications/", views.notifications_api, name="notifications_api"),
    path("api/notifications/count/", views.notifications_count_api, name="notifications_count_api"),
    path("api/notifications/mark-all-read/", views.mark_all_notifications_read, name="mark_all_read"),
]
