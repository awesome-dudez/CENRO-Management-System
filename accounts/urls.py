from django.urls import path
from django.contrib.auth import views as auth_views

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile, name="profile"),
    path("register/consumer/", views.consumer_register, name="consumer_register"),
    path("register/staff/", views.staff_register, name="staff_register"),
    path("staff/approvals/", views.staff_approval_list, name="staff_approval_list"),
]

