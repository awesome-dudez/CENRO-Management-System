import logging

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.utils import IntegrityError
from django.shortcuts import redirect, render

from .forms import ConsumerRegistrationForm, LoginForm, ProfileUpdateForm, StaffRegistrationForm
from .models import ConsumerProfile, User

logger = logging.getLogger(__name__)


def login_view(request):
    # If user is already authenticated, show dashboard (not login page)
    if request.user.is_authenticated:
        if request.user.is_admin():
            return redirect("dashboard:admin_dashboard")
        return redirect("dashboard:home")

    if request.method == "POST":
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            try:
                user = form.get_user()
                is_approved = getattr(user, "is_approved", True)
                role = getattr(user, "role", None)
                if not is_approved and role != User.Role.ADMIN and not getattr(user, "is_superuser", False):
                    messages.warning(request, "Your account is pending approval by an administrator.")
                    return redirect("accounts:login")
                login(request, user)
                if user.is_admin():
                    return redirect("dashboard:admin_dashboard")
                return redirect("dashboard:home")
            except Exception as e:
                logger.exception("Login failed after form.is_valid(): %s", e)
                messages.error(request, "An error occurred during sign in. Please try again.")
        # Invalid form: fall through to re-render with errors
    else:
        form = LoginForm(request)
    return render(request, "accounts/login.html", {"form": form})


def consumer_register(request):
    if request.method == "POST":
        form = ConsumerRegistrationForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    user = form.save()
                login(request, user)
                messages.success(request, "Registration successful! Welcome to EcoTrack.")
                if user.role == User.Role.ADMIN:
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
            except Exception as e:
                logger.exception("Registration failed: %s", e)
                messages.error(request, "An error occurred during registration. Please try again.")
        else:
            messages.error(request, "Please fix the errors below and try again.")
    else:
        form = ConsumerRegistrationForm()
    return render(request, "accounts/consumer_register.html", {"form": form})


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
def profile(request):
    user = request.user
    prof, _ = ConsumerProfile.objects.get_or_create(user=user)

    if request.method == "POST":
        form = ProfileUpdateForm(request.POST, request.FILES)
        if form.is_valid():
            user.first_name = form.cleaned_data["first_name"]
            user.last_name = form.cleaned_data["last_name"]
            user.email = form.cleaned_data["email"]
            user.save()

            if form.cleaned_data.get("profile_picture"):
                prof.profile_picture = form.cleaned_data["profile_picture"]
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
        form = ProfileUpdateForm(initial={
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
        })

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
    pending_staff = User.objects.filter(role=User.Role.STAFF, is_approved=False)
    if request.method == "POST":
        user_id = request.POST.get("user_id")
        if user_id:
            try:
                staff = pending_staff.get(id=user_id)
                staff.is_approved = True
                staff.save()
                messages.success(request, f"Staff account {staff.username} approved.")
                return redirect("accounts:staff_approval_list")
            except User.DoesNotExist:
                messages.error(request, "Staff account not found.")
    return render(request, "accounts/staff_approval_list.html", {"pending_staff": pending_staff})

