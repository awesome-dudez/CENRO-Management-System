import logging
import random

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db import DatabaseError, transaction
from django.db.utils import IntegrityError, OperationalError, ProgrammingError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import (
    ConsumerRegistrationForm,
    ForgotPasswordForm,
    LoginForm,
    ProfileUpdateForm,
    SetNewPasswordForm,
    StaffRegistrationForm,
    VerifyCodeForm,
)
from .models import ConsumerProfile, PasswordResetToken, User

logger = logging.getLogger(__name__)


def _safe_is_admin(user):
    """True if user is admin; never access .role/.is_superuser on AnonymousUser."""
    if not getattr(user, "is_authenticated", False):
        return False
    role = getattr(user, "role", None)
    if role == User.Role.ADMIN:
        return True
    if getattr(user, "is_superuser", False):
        return True
    return False


def _init_registration_captcha(request):
    """
    Store a simple math captcha in the session to deter automated registrations.
    Returns the tuple (a, b) so the template can render 'a + b'.
    """
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    request.session["registration_captcha"] = {"a": a, "b": b, "sum": a + b}
    return a, b


def _sync_legacy_record(user):
    """
    After a new consumer registers, check for a matching legacy (pre-system)
    record.  If found, merge prior desludging volume into the new profile,
    re-assign any service requests linked to the legacy account, create a
    notification for the user, and delete the legacy account.
    """
    try:
        profile = user.consumer_profile
    except ConsumerProfile.DoesNotExist:
        return

    legacy = (
        User.objects.filter(
            role=User.Role.CONSUMER,
            is_legacy_record=True,
            first_name__iexact=user.first_name.strip(),
            last_name__iexact=user.last_name.strip(),
            consumer_profile__barangay__iexact=(profile.barangay or "").strip(),
        )
        .exclude(pk=user.pk)
        .select_related("consumer_profile")
        .first()
    )
    if not legacy:
        return

    legacy_profile = legacy.consumer_profile
    update_fields = []
    if legacy_profile.prior_desludging_m3_4y and legacy_profile.prior_desludging_m3_4y > 0:
        profile.prior_desludging_m3_4y = legacy_profile.prior_desludging_m3_4y
        update_fields.append("prior_desludging_m3_4y")
    if legacy_profile.last_cycle_request_date:
        profile.last_cycle_request_date = legacy_profile.last_cycle_request_date
        update_fields.append("last_cycle_request_date")
    if update_fields:
        profile.save(update_fields=update_fields)

    from services.models import ServiceRequest, Notification

    ServiceRequest.objects.filter(consumer=legacy).update(consumer=user)

    Notification.objects.create(
        user=user,
        message=(
            "Welcome! We found an existing record matching your information. "
            "Your previous desludging history has been synced with your new account."
        ),
        notification_type=Notification.NotificationType.STATUS_CHANGE,
    )

    legacy.delete()


def login_view(request):
    # Only check authenticated; never access .role before verifying authenticated
    if getattr(request.user, "is_authenticated", False):
        try:
            if _safe_is_admin(request.user):
                return redirect("dashboard:admin_dashboard")
            return redirect("dashboard:home")
        except Exception as e:
            logger.exception("Login view redirect (authenticated user) failed: %s", e)
            messages.error(
                request,
                "We could not open your dashboard right after sign-in. Please try logging in again, or contact support if it keeps happening.",
            )

    form = LoginForm(request)
    if request.method != "POST":
        return render(request, "accounts/login.html", {"form": form})

    # Make username lookup case-insensitive while keeping password case-sensitive.
    post_data = request.POST.copy()
    raw_username = (post_data.get("username") or "").strip()
    if raw_username:
        try:
            match = User.objects.get(username__iexact=raw_username)
            post_data["username"] = match.username
        except User.DoesNotExist:
            # No matching user; keep original so Django shows standard error.
            pass

    form = LoginForm(request, data=post_data)
    try:
        is_valid = form.is_valid()
    except (OperationalError, ProgrammingError, IntegrityError) as e:
        logger.exception("Login DB error during form.is_valid() (authenticate): %s", e)
        messages.error(
            request,
            "Sign-in is temporarily unavailable because the database did not respond. Please wait a minute and try again.",
        )
        return render(request, "accounts/login.html", {"form": form})
    except Exception as e:
        logger.exception("Login view crashed during form.is_valid(): %s", e)
        messages.error(
            request,
            "Sign-in failed because of an unexpected server error. Please try again, or use Forgot password if you need to reset your password.",
        )
        return render(request, "accounts/login.html", {"form": form})

    if not is_valid:
        messages.error(
            request,
            "Sign-in did not succeed. Correct any highlighted fields — check your username and password, or use Forgot password.",
        )
        return render(request, "accounts/login.html", {"form": form})

    try:
        user = form.get_user()
    except (OperationalError, ProgrammingError, IntegrityError) as e:
        logger.exception("Login DB error during get_user: %s", e)
        messages.error(
            request,
            "Sign-in is temporarily unavailable because the database did not respond. Please wait a minute and try again.",
        )
        return render(request, "accounts/login.html", {"form": form})
    except Exception as e:
        logger.exception("Login failed during get_user: %s", e)
        messages.error(
            request,
            "Sign-in could not be completed because of a server error. Please try again in a moment.",
        )
        return render(request, "accounts/login.html", {"form": form})

    is_approved = getattr(user, "is_approved", True)
    role = getattr(user, "role", None)
    if not is_approved and role != User.Role.ADMIN and not getattr(user, "is_superuser", False):
        messages.warning(request, "Your account is pending approval by an administrator.")
        return render(request, "accounts/login.html", {"form": form})

    try:
        login(request, user)
    except Exception as e:
        logger.exception("Login session save failed: %s", e)
        messages.error(
            request,
            "Your password was accepted but the session could not be saved (browser or server issue). Try again, or use a different browser.",
        )
        return render(request, "accounts/login.html", {"form": form})

    try:
        # If staff or consumer was given a temporary password, force password change first.
        if getattr(user, "must_change_password", False) and getattr(user, "role", None) in (
            User.Role.STAFF,
            User.Role.CONSUMER,
        ):
            return redirect("accounts:force_password_change")

        if _safe_is_admin(user):
            return redirect("dashboard:admin_dashboard")
        return redirect("dashboard:home")
    except Exception as e:
        logger.exception("Login redirect after auth failed: %s", e)
        return redirect("dashboard:home")


def consumer_register(request):
    if request.method != "POST":
        a, b = _init_registration_captcha(request)
        form = ConsumerRegistrationForm()
        return render(
            request,
            "accounts/consumer_register.html",
            {"form": form, "captcha_a": a, "captcha_b": b},
        )

    captcha_data = request.session.get("registration_captcha") or {}

    form = ConsumerRegistrationForm(request.POST)
    try:
        is_valid = form.is_valid()
    except (OperationalError, ProgrammingError, IntegrityError) as e:
        logger.exception("Register view crashed during form.is_valid(): %s", e)
        messages.error(
            request,
            "Registration is temporarily unavailable because the database did not respond. Please wait a minute and try again.",
        )
        a, b = _init_registration_captcha(request)
        return render(
            request,
            "accounts/consumer_register.html",
            {"form": form, "captcha_a": a, "captcha_b": b},
        )
    except Exception as e:
        logger.exception("Register view crashed: %s", e)
        messages.error(
            request,
            "Registration could not continue because of an unexpected server error. Please try again, or contact support.",
        )
        a, b = _init_registration_captcha(request)
        return render(
            request,
            "accounts/consumer_register.html",
            {"form": form, "captcha_a": a, "captcha_b": b},
        )

    # Additional security checks after built-in validation (must run on every POST; was previously dead code).
    # 1) Honeypot (bots that fill hidden field)
    honeypot_val = (request.POST.get("website") or "").strip()
    if honeypot_val:
        is_valid = False
        form.add_error(None, "Registration blocked for security reasons.")

    # 2) Math captcha — compare POST answer to the challenge stored in session for this browser.
    expected_sum = captcha_data.get("sum")
    a_sess = captcha_data.get("a")
    b_sess = captcha_data.get("b")
    raw_answer = request.POST.get("captcha_answer", "").strip()
    try:
        answer = int(raw_answer)
    except (TypeError, ValueError):
        answer = None
    if expected_sum is None or a_sess is None or b_sess is None:
        is_valid = False
        form.add_error(
            "captcha_answer",
            "Security check expired or missing. Please refresh the page and solve the new question.",
        )
    elif answer != expected_sum:
        is_valid = False
        form.add_error("captcha_answer", "Incorrect answer. Please try again.")

    if not is_valid:
        a, b = _init_registration_captcha(request)
        messages.error(
            request,
            "Please review the highlighted fields (username, password rules, email, mobile number, or captcha) and try again.",
        )
        return render(
            request,
            "accounts/consumer_register.html",
            {"form": form, "captcha_a": a, "captcha_b": b},
        )

    try:
        with transaction.atomic():
            user = form.save()
            _sync_legacy_record(user)
        login(request, user)
        request.session.pop("registration_captcha", None)
        messages.success(request, "Registration successful! Welcome to the CENRO Sanitary Management System.")
        role = getattr(user, "role", None)
        if role == User.Role.ADMIN:
            return redirect("dashboard:admin_dashboard")
        # Let other browser tabs (e.g. service request wizard) detect successful signup via bridge page.
        request.session["_consumer_reg_notify_pending"] = True
        if getattr(user, "must_change_password", False) and role == User.Role.CONSUMER:
            return redirect("accounts:force_password_change")
        return redirect("accounts:consumer_register_complete_notify")
    except IntegrityError as e:
        logger.exception("Registration IntegrityError: %s", e)
        if "username" in str(e).lower() or "unique" in str(e).lower():
            form.add_error("username", "This username is already taken. Please choose another.")
        elif "email" in str(e).lower():
            form.add_error("email", "An account with this email already exists.")
        else:
            form.add_error(None, "Username or email already in use. Please choose different values.")
        a, b = _init_registration_captcha(request)
        return render(
            request,
            "accounts/consumer_register.html",
            {"form": form, "captcha_a": a, "captcha_b": b},
        )
    except (OperationalError, ProgrammingError) as e:
        logger.exception("Registration DB error: %s", e)
        messages.error(
            request,
            "We could not save your account because the database did not respond. Please wait a minute and try again.",
        )
        a, b = _init_registration_captcha(request)
        return render(
            request,
            "accounts/consumer_register.html",
            {"form": form, "captcha_a": a, "captcha_b": b},
        )
    except Exception as e:
        logger.exception("Registration failed: %s", e)
        messages.error(
            request,
            "Registration could not be completed because of a server error. Please try again, or contact the administrator.",
        )
        a, b = _init_registration_captcha(request)
        return render(
            request,
            "accounts/consumer_register.html",
            {"form": form, "captcha_a": a, "captcha_b": b},
        )


@login_required
def staff_register(request):
    if not request.user.is_superuser and request.user.role != User.Role.ADMIN:
        messages.error(request, "Only admins can create staff accounts.")
        return redirect("dashboard:home")
    if request.method == "POST":
        form = StaffRegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Staff account created. Awaiting admin approval.")
            return redirect("dashboard:home")
    else:
        form = StaffRegistrationForm()
    return render(request, "accounts/staff_register.html", {"form": form})


@login_required
def force_password_change(request):
    """Force staff or consumer to change their temporary password (e.g. after admin reset)."""
    user = request.user
    # Only enforced for accounts that still require a change.
    if getattr(user, "role", None) not in (User.Role.STAFF, User.Role.CONSUMER) or not getattr(
        user, "must_change_password", False
    ):
        # Nothing to do; send them to their normal landing page.
        if _safe_is_admin(user):
            return redirect("dashboard:admin_dashboard")
        return redirect("dashboard:home")

    if request.method == "POST":
        form = PasswordChangeForm(user, request.POST)
        if form.is_valid():
            updated_user = form.save()
            # Keep user logged in after password change
            update_session_auth_hash(request, updated_user)
            updated_user.must_change_password = False
            updated_user.save(update_fields=["password", "must_change_password"])
            messages.success(request, "Your password has been updated.")
            if _safe_is_admin(updated_user):
                return redirect("dashboard:admin_dashboard")
            if request.session.get("_consumer_reg_notify_pending"):
                return redirect("accounts:consumer_register_complete_notify")
            return redirect("dashboard:home")
    else:
        form = PasswordChangeForm(user)

    return render(request, "accounts/force_password_change.html", {"form": form})


@login_required
def consumer_register_complete_notify(request):
    """
    Minimal page shown once after consumer self-registration.
    Notifies other tabs (BroadcastChannel / localStorage) then sends the user to the dashboard.
    """
    if not request.session.pop("_consumer_reg_notify_pending", False):
        return redirect("dashboard:home")
    return render(
        request,
        "accounts/consumer_register_complete_notify.html",
        {
            "dashboard_url": reverse("dashboard:home"),
            "service_request_create_url": reverse("services:create_request"),
        },
    )


@login_required
def profile(request):
    user = request.user
    prof, _ = ConsumerProfile.objects.get_or_create(user=user)

    if request.method == "POST":
        form = ProfileUpdateForm(request.POST, request.FILES, user=user, consumer_profile=prof)
        if form.is_valid():
            user.first_name = form.cleaned_data["first_name"]
            user.last_name = form.cleaned_data["last_name"]
            user.email = form.cleaned_data["email"]
            user.save()

            # File may be in cleaned_data or FILES (avoid relying only on cleaned_data).
            # Note: file inputs inside display:none are often omitted by browsers from POST.
            uploaded_pic = form.cleaned_data.get("profile_picture") or request.FILES.get(
                "profile_picture"
            )
            if uploaded_pic:
                prof.profile_picture = uploaded_pic
            prof.gender = form.cleaned_data.get("gender") or "MALE"
            prof.birthdate = form.cleaned_data.get("birthdate")
            prof.mobile_number = form.cleaned_data["mobile_number"]
            prof.street_address = form.cleaned_data.get("street_address") or ""
            prof.barangay = form.cleaned_data["barangay"]
            prof.municipality = form.cleaned_data["municipality"]
            prof.province = form.cleaned_data["province"]
            prof.save()

            messages.success(request, "Your profile has been updated.")
            return redirect("accounts:profile")
    else:
        form = ProfileUpdateForm(
            initial={
                "first_name": user.first_name,
                "last_name": user.last_name,
                "gender": prof.gender,
                "birthdate": prof.birthdate.isoformat() if prof.birthdate else "",
                "email": user.email,
                "mobile_number": prof.mobile_number,
                "street_address": prof.street_address,
                "barangay": prof.barangay,
                "municipality": prof.municipality,
                "province": prof.province,
            },
            user=user,
            consumer_profile=prof,
        )

    return render(request, "accounts/profile.html", {"form": form, "profile": prof})


@login_required
def logout_view(request):
    logout(request)
    return redirect("accounts:login")


# ---------------------------------------------------------------------------
# Password Reset via secure database token
# ---------------------------------------------------------------------------

def forgot_password_view(request):
    """Step 1 — user enters email; we create a DB token and email a reset link."""
    try:
        return _forgot_password_view_inner(request)
    except Exception as e:
        # Never return a bare 500 for this flow: log and show a recoverable page (Render / DB / edge cases).
        logger.exception("forgot_password_view: unhandled error: %s", e)
        messages.error(
            request,
            "Something went wrong on this page. Please try again in a moment. "
            "If the problem continues, contact the administrator (check database connectivity and migrations on the server).",
        )
        return render(request, "accounts/forgot_password.html", {"form": ForgotPasswordForm()})


def _forgot_password_view_inner(request):
    if request.method == "POST":
        form = ForgotPasswordForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data["username"].strip()
            email = form.cleaned_data["email"].strip().lower()

            # Check username exists first
            try:
                user_by_username = User.objects.get(username__iexact=username)
            except User.DoesNotExist:
                user_by_username = None
            except DatabaseError as e:
                logger.exception("Password reset: database error on username lookup: %s", e)
                messages.error(
                    request,
                    "The server could not verify your account right now (database error). "
                    "Please try again shortly. Administrators should confirm DATABASE_URL and run migrations.",
                )
                return render(request, "accounts/forgot_password.html", {"form": form})

            if user_by_username is None:
                form.add_error("username", "No account found with this username.")
                return render(request, "accounts/forgot_password.html", {"form": form})

            account_email = (user_by_username.email or "").strip().lower()
            if not account_email:
                form.add_error(
                    "email",
                    "This account has no email address on file. Please contact the administrator for help.",
                )
                return render(request, "accounts/forgot_password.html", {"form": form})

            # Check email matches that account
            if account_email != email:
                form.add_error("email", "This email does not match the account for that username.")
                return render(request, "accounts/forgot_password.html", {"form": form})

            user = user_by_username

            try:
                # Cooldown: one token per 60 seconds per user to prevent spam
                latest = (
                    PasswordResetToken.objects.filter(user=user, is_used=False)
                    .order_by("-created_at")
                    .first()
                )
                if latest and (timezone.now() - latest.created_at).total_seconds() < 60:
                    messages.warning(
                        request,
                        "A reset email was already sent recently. Please wait a moment before requesting another.",
                    )
                    return render(request, "accounts/forgot_password.html", {"form": form})

                reset_token = PasswordResetToken.create_for_user(user, minutes=15)
                reset_path = reverse(
                    "accounts:reset_password",
                    kwargs={"token": reset_token.token},
                )
                reset_url = request.build_absolute_uri(reset_path)
                verify_url = request.build_absolute_uri(reverse("accounts:verify_code"))

                try:
                    send_mail(
                        subject="CENRO Sanitary Management System — Password Reset",
                        message=(
                            f"Hello {user.get_full_name() or user.username},\n\n"
                            f"We received a request to reset your CENRO Sanitary Management System password.\n\n"
                            f"Please reset your password by clicking this link:\n"
                            f"  {reset_url}\n\n"
                            f"Or enter this one-time verification code on the site:\n"
                            f"  {reset_token.code}\n\n" 
                            f"To enter the code manually, go to:\n"
                            f"  {verify_url}\n\n"
                            f"This code expires in 15 minutes.\n\n"
                            f"If you did not request a password reset, you can safely ignore this email.\n\n"
                            f"— CENRO Sanitary Management System, Bayawan City"
                        ),
                        from_email=django_settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[account_email],
                        fail_silently=False,
                    )
                except Exception as e:
                    logger.exception("Failed to send password reset email: %s", e)
                    messages.error(
                        request,
                        "Could not send the email. The server mail settings may be incomplete on this deployment. "
                        "If you are the site administrator, set EMAIL_HOST_USER and EMAIL_HOST_PASSWORD (Gmail app password) "
                        "or EMAIL_BACKEND in Render environment variables.",
                    )
                    return render(request, "accounts/forgot_password.html", {"form": form})

                messages.success(
                    request,
                    "If that email address is registered, a password reset email has been sent. "
                    "Check your inbox (and spam folder), then enter the code below.",
                )
                return redirect("accounts:verify_code")
            except Exception as e:
                logger.exception("Password reset request failed: %s", e)
                messages.error(
                    request,
                    "We could not create a password reset link right now (server or database issue). "
                    "Please try again in a few minutes. If it keeps failing, contact the CENRO administrator.",
                )
                return render(request, "accounts/forgot_password.html", {"form": form})
    else:
        form = ForgotPasswordForm()

    return render(request, "accounts/forgot_password.html", {"form": form})


def verify_code_view(request):
    """Alternative step: user enters the 6-digit code from the email instead of clicking the link."""
    if request.method == "POST":
        form = VerifyCodeForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["code"]
            try:
                reset_token = (
                    PasswordResetToken.objects
                    .select_related("user")
                    .filter(code=code, is_used=False)
                    .order_by("-created_at")
                    .first()
                )
            except Exception:
                reset_token = None

            if reset_token is None or not reset_token.is_valid():
                form.add_error("code", "This code is invalid or has expired. Please request a new reset email.")
            else:
                return redirect("accounts:reset_password", token=reset_token.token)
    else:
        form = VerifyCodeForm()

    return render(request, "accounts/verify_code.html", {"form": form})


def reset_password_view(request, token: str):
    """Step 2 — validate token from URL, let user set a new password."""
    try:
        reset_token = PasswordResetToken.objects.select_related("user").get(token=token)
    except PasswordResetToken.DoesNotExist:
        messages.error(request, "This password reset link is invalid or has already been used.")
        return redirect("accounts:forgot_password")

    if not reset_token.is_valid():
        messages.error(
            request,
            "This password reset link has expired or already been used. Please request a new one.",
        )
        return redirect("accounts:forgot_password")

    if request.method == "POST":
        form = SetNewPasswordForm(request.POST)
        if form.is_valid():
            user = reset_token.user
            user.set_password(form.cleaned_data["new_password1"])
            user.must_change_password = False
            user.save(update_fields=["password", "must_change_password"])

            # Invalidate the token so it cannot be reused
            reset_token.invalidate()

            messages.success(
                request,
                "Your password has been reset successfully. Please sign in with your new password.",
            )
            return redirect("accounts:login")
    else:
        form = SetNewPasswordForm()

    return render(request, "accounts/reset_password_new.html", {
        "form": form,
        "token": token,
    })


@login_required
def staff_approval_list(request):
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        messages.error(request, "Only admins can approve staff accounts.")
        return redirect("dashboard:home")
    from services.models import DesludgingPersonnel

    all_staff = User.objects.filter(role=User.Role.STAFF).order_by("username")
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "add_personnel":
            full_name = (request.POST.get("personnel_full_name") or "").strip()
            role = (request.POST.get("personnel_role") or "").strip().upper()
            if not full_name:
                messages.error(request, "Enter a name for the driver or helper.")
            elif role not in (DesludgingPersonnel.Role.DRIVER, DesludgingPersonnel.Role.HELPER):
                messages.error(request, "Choose Driver or Helper.")
            else:
                DesludgingPersonnel.objects.create(full_name=full_name, role=role)
                messages.success(request, f"Added {full_name} as {role.title().lower()}.")
            return redirect("accounts:staff_approval_list")

        if action == "delete_personnel":
            pid = request.POST.get("personnel_id")
            try:
                p = DesludgingPersonnel.objects.get(pk=int(pid))
            except (ValueError, TypeError, DesludgingPersonnel.DoesNotExist):
                messages.error(request, "Personnel entry not found.")
            else:
                label = str(p)
                p.delete()
                messages.success(request, f"Removed {label}.")
            return redirect("accounts:staff_approval_list")

        user_id = request.POST.get("user_id")
        if not user_id:
            messages.error(request, "No staff account specified.")
            return redirect("accounts:staff_approval_list")
        try:
            staff = User.objects.get(id=user_id, role=User.Role.STAFF)
        except User.DoesNotExist:
            messages.error(request, "Staff account not found.")
            return redirect("accounts:staff_approval_list")

        if action == "set_staff_status":
            status = (request.POST.get("status") or "").strip().lower()
            if status == "active":
                staff.is_approved = True
                staff.is_active = True
                staff.save(update_fields=["is_approved", "is_active"])
                messages.success(
                    request,
                    f"Staff account {staff.username} is now Active.",
                )
            elif status == "inactive":
                if staff == request.user:
                    messages.error(
                        request,
                        "You cannot set your own account to Inactive while logged in.",
                    )
                else:
                    staff.is_active = False
                    staff.save(update_fields=["is_active"])
                    messages.success(
                        request,
                        f"Staff account {staff.username} is now Inactive and cannot sign in.",
                    )
            else:
                messages.error(request, "Invalid status value.")
            return redirect("accounts:staff_approval_list")

        if action == "delete":
            if staff == request.user:
                messages.error(request, "You cannot delete your own staff account while logged in.")
                return redirect("accounts:staff_approval_list")
            confirm_text = (request.POST.get("confirm_text") or "").strip().upper()
            if confirm_text != "DELETE":
                messages.error(request, 'To confirm deletion, type "DELETE" in the confirmation box.')
                return redirect("accounts:staff_approval_list")
            username = staff.username
            staff.delete()
            messages.success(request, f"Staff account {username} has been deleted.")
            return redirect("accounts:staff_approval_list")

        messages.error(
            request,
            "That staff-management action was not recognized. Refresh the page and try again, or use the menu links.",
        )
        return redirect("accounts:staff_approval_list")

    drivers = DesludgingPersonnel.objects.filter(
        role=DesludgingPersonnel.Role.DRIVER, is_active=True
    ).order_by("full_name")
    helpers = DesludgingPersonnel.objects.filter(
        role=DesludgingPersonnel.Role.HELPER, is_active=True
    ).order_by("full_name")
    return render(
        request,
        "accounts/staff_approval_list.html",
        {
            "all_staff": all_staff,
            "personnel_drivers": drivers,
            "personnel_helpers": helpers,
        },
    )

