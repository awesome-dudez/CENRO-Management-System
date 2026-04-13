from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods

from services.models import ServiceRequest
from scheduling.models import Schedule


def home(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")

    if request.user.is_admin():
        return redirect("dashboard:admin_dashboard")

    # Staff should not create their own requests; send them to the
    # admin Requests view where they can only see assigned requests.
    if hasattr(request.user, "is_staff_member") and request.user.is_staff_member():
        return redirect("dashboard:admin_requests")

    # For regular consumers, show their own requests plus any in‑progress
    # requests they submitted on behalf of another person.
    user_requests = ServiceRequest.objects.filter(
        Q(consumer=request.user)
        | Q(
            requested_by=request.user,
            status__in=[
                ServiceRequest.Status.SUBMITTED,
                ServiceRequest.Status.UNDER_REVIEW,
                ServiceRequest.Status.INSPECTION_SCHEDULED,
                ServiceRequest.Status.INSPECTED,
                ServiceRequest.Status.COMPUTATION_SENT,
                ServiceRequest.Status.AWAITING_PAYMENT,
                ServiceRequest.Status.PAID,
                ServiceRequest.Status.DESLUDGING_SCHEDULED,
                ServiceRequest.Status.GRASS_PENDING_PAYMENT,
                ServiceRequest.Status.GRASS_PAYMENT_AWAITING_VERIFICATION,
            ],
        )
    ).order_by("-created_at")

    pending_count = user_requests.exclude(
        status__in=[
            ServiceRequest.Status.COMPLETED,
            ServiceRequest.Status.CANCELLED,
        ]
    ).count()
    completed_count = user_requests.filter(status=ServiceRequest.Status.COMPLETED).count()
    total_count = user_requests.count()

    today = date.today()
    upcoming_schedules = (
        Schedule.objects.filter(
            Q(service_request__consumer=request.user)
            | Q(service_request__requested_by=request.user),
            service_date__gte=today,
            service_date__lte=today + timedelta(days=30),
        )
        .select_related("service_request", "assigned_staff")
        .order_by("service_date")
    )

    context = {
        "requests": user_requests,
        "pending_count": pending_count,
        "completed_count": completed_count,
        "total_count": total_count,
        "upcoming_schedules": upcoming_schedules,
    }
    return render(request, "dashboard/home.html", context)
