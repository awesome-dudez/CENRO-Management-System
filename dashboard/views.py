from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods

from services.models import ServiceRequest
from scheduling.models import Schedule


def home(request):
    if not request.user.is_authenticated:
        return redirect('accounts:login')

    if request.user.is_admin():
        return redirect('dashboard:admin_dashboard')

    user_requests = ServiceRequest.objects.filter(consumer=request.user).order_by("-created_at")
    pending_count = user_requests.filter(status=ServiceRequest.Status.SUBMITTED).count()
    completed_count = user_requests.filter(status=ServiceRequest.Status.COMPLETED).count()
    total_count = user_requests.count()

    today = date.today()
    upcoming_schedules = (
        Schedule.objects.filter(
            service_request__consumer=request.user,
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
