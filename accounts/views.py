import logging
import random

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.utils import IntegrityError, OperationalError, ProgrammingError
from django.shortcuts import redirect, render

from .forms import ConsumerRegistrationForm, LoginForm, ProfileUpdateForm, StaffRegistrationForm
from .models import ConsumerProfile, User

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


def login_view(request):
    # Only check authenticated; never access .role before verifying authenticated
    if getattr(request.user, "is_authenticated", False):
        try:
            if _safe_is_admin(request.user):
                return redirect("dashboard:admin_dashboard")
            return redirect("dashboard:home")
        except Exception as e:
            logger.exception("Login view redirect (authenticated user) failed: %s", e)
            messages.error(request, "An error occurred. Please try again.")

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
        messages.error(request, "A temporary error occurred. Please try again in a moment.")
        return render(request, "accounts/login.html", {"form": form})
    except Exception as e:
        logger.exception("Login view crashed during form.is_valid(): %s", e)
        messages.error(request, "An error occurred during sign in. Please try again.")
        return render(request, "accounts/login.html", {"form": form})

    if not is_valid:
        messages.error(request, "Please fix the errors below.")
        return render(request, "accounts/login.html", {"form": form})

    try:
        user = form.get_user()
    except (OperationalError, ProgrammingError, IntegrityError) as e:
        logger.exception("Login DB error during get_user: %s", e)
        messages.error(request, "A temporary error occurred. Please try again in a moment.")
        return render(request, "accounts/login.html", {"form": form})
    except Exception as e:
        logger.exception("Login failed during get_user: %s", e)
        messages.error(request, "An error occurred during sign in. Please try again.")
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
        messages.error(request, "An error occurred during sign in. Please try again.")
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
    a = captcha_data.get("a") or 0
    b = captcha_data.get("b") or 0

    form = ConsumerRegistrationForm(request.POST)
    try:
        is_valid = form.is_valid()
    except (OperationalError, ProgrammingError, IntegrityError) as e:
        logger.exception("Register view crashed during form.is_valid(): %s", e)
        messages.error(request, "A temporary error occurred. Please try again.")
        return render(request, "accounts/consumer_register.html", {"form": form})
    except Exception as e:
        logger.exception("Register view crashed: %s", e)
        messages.error(request, "An error occurred. Please try again.")
        return render(request, "accounts/consumer_register.html", {"form": form})

        # Additional security checks after built-in validation:
        # 1) Honeypot (bots that fill hidden field)
        honeypot_val = (form.cleaned_data.get("website") or "").strip() if hasattr(form, "cleaned_data") else ""
        if honeypot_val:
            is_valid = False
            form.add_error(None, "Registration blocked for security reasons.")

        # 2) Simple math captcha to reduce automated sign-ups
        expected_sum = captcha_data.get("sum")
        raw_answer = request.POST.get("captcha_answer", "").strip()
        try:
            answer = int(raw_answer)
        except (TypeError, ValueError):
            answer = None
        if expected_sum is None or answer is None or answer != expected_sum:
            is_valid = False
            form.add_error("captcha_answer", "Incorrect answer. Please try again.")
    if not is_valid:
        a, b = _init_registration_captcha(request)
        messages.error(request, "Please fix the errors below and try again.")
        return render(
            request,
            "accounts/consumer_register.html",
            {"form": form, "captcha_a": a, "captcha_b": b},
        )

    try:
        with transaction.atomic():
            user = form.save()
        login(request, user)
        # Clear captcha once successfully used
        request.session.pop("registration_captcha", None)
        messages.success(request, "Registration successful! Welcome to the CENRO Management System.")
        role = getattr(user, "role", None)
        if role == User.Role.ADMIN:
            return redirect("dashboard:admin_dashboard")
        return redirect("dashboard:home")
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
        messages.error(request, "A temporary error occurred. Please try again in a moment.")
        a, b = _init_registration_captcha(request)
        return render(
            request,
            "accounts/consumer_register.html",
            {"form": form, "captcha_a": a, "captcha_b": b},
        )
    except Exception as e:
        logger.exception("Registration failed: %s", e)
        messages.error(request, "An error occurred during registration. Please try again.")
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
            return redirect("dashboard:home")
    else:
        form = PasswordChangeForm(user)

    return render(request, "accounts/force_password_change.html", {"form": form})


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


@login_required
def staff_approval_list(request):
    if request.user.role != User.Role.ADMIN and not request.user.is_superuser:
        messages.error(request, "Only admins can approve staff accounts.")
        return redirect("dashboard:home")
    all_staff = User.objects.filter(role=User.Role.STAFF).order_by("username")
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
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

        messages.error(request, "Unknown action.")
        return redirect("accounts:staff_approval_list")

    return render(
        request,
        "accounts/staff_approval_list.html",
        {"all_staff": all_staff},
    )

