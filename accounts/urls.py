from django.urls import path
from django.contrib.auth import views as auth_views

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile, name="profile"),
    path("profile/verify-contact/", views.profile_verify_contact, name="profile_verify_contact"),
    path("register/consumer/", views.consumer_register, name="consumer_register"),
    path(
        "register/consumer/complete-notify/",
        views.consumer_register_complete_notify,
        name="consumer_register_complete_notify",
    ),
    path("register/staff/", views.staff_register, name="staff_register"),
    path("staff/approvals/", views.staff_approval_list, name="staff_approval_list"),
    path("staff/change-password/", views.force_password_change, name="force_password_change"),
    # Password reset via secure database token + 6-digit code
    path("forgot-password/", views.forgot_password_view, name="forgot_password"),
    path("verify-code/", views.verify_code_view, name="verify_code"),
    path("reset-password/<str:token>/", views.reset_password_view, name="reset_password"),
]

