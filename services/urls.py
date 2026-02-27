from django.urls import path

from . import views

app_name = "services"

urlpatterns = [
    path("request/create/", views.create_request, name="create_request"),
    path("api/reverse-geocode/", views.reverse_geocode, name="reverse_geocode"),
    path("request/<int:pk>/", views.request_detail, name="request_detail"),
    path("requests/", views.request_list, name="request_list"),
    path("history/", views.history, name="history"),
    path("clients/", views.client_records, name="client_records"),
    path("request/<int:pk>/inspect/", views.submit_inspection, name="submit_inspection"),
    path("request/<int:pk>/complete-info/", views.submit_completion, name="submit_completion"),
    path("request/<int:pk>/computation/", views.view_computation, name="view_computation"),
    path("request/<int:pk>/upload-receipt/", views.upload_receipt, name="upload_receipt"),
    path("request/<int:pk>/print/", views.print_application, name="print_application"),
    path("request/<int:pk>/complete/", views.complete_request, name="complete_request"),
    path("notifications/", views.notification_list, name="notification_list"),
    path("notifications/mark-read/<int:pk>/", views.mark_notification_read, name="mark_notification_read"),
]
