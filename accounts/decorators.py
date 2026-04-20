from functools import wraps
from typing import Callable

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect

from .models import User

_ROLE_PAGE_LABELS = {
    "ADMIN": "Administrator",
    "STAFF": "CENRO field staff / inspector",
    "CONSUMER": "Resident (consumer) account",
}


def role_required(*roles: str) -> Callable:
    def decorator(view_func: Callable) -> Callable:
        @login_required
        @wraps(view_func)
        def _wrapped_view(request: HttpRequest, *args, **kwargs) -> HttpResponse:
            user: User = request.user  # type: ignore[assignment]
            if not user.is_authenticated:
                return redirect("accounts:login")
            # Superuser (super admin) can access any role-protected page
            if not user.is_superuser and user.role not in roles:
                allowed = ", ".join(
                    _ROLE_PAGE_LABELS.get(r, str(r).replace("_", " ").title()) for r in roles
                )
                messages.error(
                    request,
                    f"This page is restricted to: {allowed}. "
                    "Sign in with an account that has access, or use the main menu to open pages for your role.",
                )
                return redirect("dashboard:home")
            if not user.is_approved and user.role != User.Role.ADMIN and not user.is_superuser:
                messages.warning(request, "Your account is pending approval from an administrator.")
                return redirect("dashboard:home")
            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator


def json_consumer_required(view_func: Callable) -> Callable:
    """
    Like role_required(CONSUMER) but returns JSON for API endpoints instead of redirecting.
    Browsers following redirects would receive HTML (e.g. dashboard) and break fetch().json().
    """

    @wraps(view_func)
    def _wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        user: User = request.user  # type: ignore[assignment]
        if not user.is_authenticated:
            return JsonResponse(
                {"ok": False, "code": "auth", "message": "Please sign in again."},
                status=401,
            )
        if not user.is_superuser and user.role != User.Role.CONSUMER:
            return JsonResponse(
                {
                    "ok": False,
                    "code": "forbidden",
                    "message": "Only resident (consumer) accounts can verify a client profile here.",
                },
                status=403,
            )
        if not user.is_approved and user.role != User.Role.ADMIN and not user.is_superuser:
            return JsonResponse(
                {
                    "ok": False,
                    "code": "unapproved",
                    "message": "Your account is pending approval. You cannot use this action until an administrator approves it.",
                },
                status=403,
            )
        return view_func(request, *args, **kwargs)

    return _wrapped

