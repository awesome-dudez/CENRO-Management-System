from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.utils import timezone

from accounts.decorators import role_required
from accounts.models import User
from services.models import ServiceRequest, Notification
from scheduling.models import Schedule


@login_required
@role_required("ADMIN")
def admin_dashboard(request):
    """Main admin dashboard with charts and statistics"""
    today = date.today()
    start_of_month = today.replace(day=1)

    total_requests = ServiceRequest.objects.count()
    pending_count = ServiceRequest.objects.filter(
        status__in=[ServiceRequest.Status.SUBMITTED, ServiceRequest.Status.UNDER_REVIEW]
    ).count()
    completed_this_month = ServiceRequest.objects.filter(
        status=ServiceRequest.Status.COMPLETED,
        request_date__gte=start_of_month,
    ).count()

    last_month_start = (start_of_month - timedelta(days=1)).replace(day=1)
    last_month_completed = ServiceRequest.objects.filter(
        status=ServiceRequest.Status.COMPLETED,
        request_date__gte=last_month_start,
        request_date__lt=start_of_month,
    ).count()

    efficiency_change = 0
    if last_month_completed > 0:
        efficiency_change = ((completed_this_month - last_month_completed) / last_month_completed) * 100

    weeks_data = []
    for i in range(4):
        week_start = start_of_month + timedelta(weeks=i)
        week_end = week_start + timedelta(days=6)
        incoming = ServiceRequest.objects.filter(
            created_at__date__gte=week_start,
            created_at__date__lte=week_end,
        ).count()
        completed = ServiceRequest.objects.filter(
            status=ServiceRequest.Status.COMPLETED,
            request_date__gte=week_start,
            request_date__lte=week_end,
        ).count()
        weeks_data.append({"week": f"Week {i+1}", "incoming": incoming, "completed": completed})

    import json
    weeks_data_json = json.dumps(weeks_data)

    residential = ServiceRequest.objects.filter(
        service_type=ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
    ).count()
    commercial = ServiceRequest.objects.filter(
        service_type=ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ).count()
    grass = ServiceRequest.objects.filter(
        service_type=ServiceRequest.ServiceType.GRASS_CUTTING,
    ).count()
    total_services = residential + commercial + grass

    residential_pct = (residential / total_services * 100) if total_services > 0 else 0
    commercial_pct = (commercial / total_services * 100) if total_services > 0 else 0

    barangay_stats = (
        ServiceRequest.objects.values("barangay")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    context = {
        "total_requests": total_requests,
        "pending_count": pending_count,
        "completed_this_month": completed_this_month,
        "efficiency_change": efficiency_change,
        "weeks_data": weeks_data_json,
        "residential_pct": round(residential_pct, 1),
        "commercial_pct": round(commercial_pct, 1),
        "barangay_stats": list(barangay_stats),
        "total_services": total_services,
    }
    return render(request, "dashboard/admin_dashboard.html", context)


@login_required
@role_required("ADMIN")
def admin_requests(request):
    """Admin requests view with sub-tabs"""
    tab = request.GET.get("tab", "pending")

    if tab == "pending":
        requests_qs = ServiceRequest.objects.filter(
            status__in=[ServiceRequest.Status.SUBMITTED, ServiceRequest.Status.UNDER_REVIEW]
        ).order_by("-created_at")
    elif tab == "inspection":
        requests_qs = ServiceRequest.objects.filter(
            status__in=[
                ServiceRequest.Status.INSPECTION_SCHEDULED,
                ServiceRequest.Status.INSPECTED,
            ]
        ).order_by("-created_at")
    elif tab == "computation":
        requests_qs = ServiceRequest.objects.filter(
            status__in=[
                ServiceRequest.Status.COMPUTATION_SENT,
                ServiceRequest.Status.AWAITING_PAYMENT,
            ]
        ).order_by("-created_at")
    elif tab == "schedule":
        requests_qs = ServiceRequest.objects.filter(
            status__in=[
                ServiceRequest.Status.PAID,
                ServiceRequest.Status.DESLUDGING_SCHEDULED,
            ]
        ).order_by("-created_at")
    elif tab == "completed":
        requests_qs = ServiceRequest.objects.filter(
            status=ServiceRequest.Status.COMPLETED,
        ).order_by("-request_date")
    else:
        requests_qs = ServiceRequest.objects.all().order_by("-created_at")

    context = {
        "requests": requests_qs,
        "active_tab": tab,
    }
    return render(request, "dashboard/admin_requests.html", context)


@login_required
@role_required("ADMIN")
def approve_request(request, pk):
    """Move a submitted request to under review"""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if request.method == "POST":
        service_request.status = ServiceRequest.Status.UNDER_REVIEW
        service_request.save()
        messages.success(request, f"Request {service_request.id} is now under review.")
        Notification.objects.create(
            user=service_request.consumer,
            message=f"Your service request #{service_request.id} is now under review.",
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )
    return redirect("dashboard:admin_requests")


@login_required
@role_required("ADMIN")
def assign_inspector(request, pk):
    """Assign inspector and inspection date to a request."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if request.method == "POST":
        inspector_id = request.POST.get("inspector_id")
        insp_date = request.POST.get("inspection_date")
        if inspector_id and insp_date:
            inspector = get_object_or_404(User, pk=inspector_id)
            service_request.assigned_inspector = inspector
            service_request.inspection_date = insp_date
            service_request.status = ServiceRequest.Status.INSPECTION_SCHEDULED
            service_request.save()
            Notification.objects.create(
                user=service_request.consumer,
                message=(
                    f"Inspector {inspector.get_full_name()} has been assigned to your request. "
                    f"Inspection date: {insp_date}."
                ),
                notification_type=Notification.NotificationType.INSPECTOR_ASSIGNED,
                related_request=service_request,
            )
            messages.success(request, "Inspector assigned and customer notified.")
        else:
            messages.error(request, "Please provide both inspector and date.")
        return redirect("services:request_detail", pk=pk)

    staff = User.objects.filter(role__in=[User.Role.ADMIN, User.Role.STAFF])
    return render(request, "dashboard/assign_inspector.html", {
        "service_request": service_request,
        "staff_members": staff,
    })


@login_required
@role_required("ADMIN")
def schedule_desludging(request, pk):
    """Admin sets the desludging date after payment is confirmed."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if request.method == "POST":
        sched_date = request.POST.get("desludging_date")
        if sched_date:
            service_request.scheduled_desludging_date = sched_date
            service_request.status = ServiceRequest.Status.DESLUDGING_SCHEDULED
            service_request.save()
            Notification.objects.create(
                user=service_request.consumer,
                message=f"Your desludging has been scheduled for {sched_date}.",
                notification_type=Notification.NotificationType.DESLUDGING_SCHEDULED,
                related_request=service_request,
            )
            messages.success(request, "Desludging date scheduled and customer notified.")
        return redirect("services:request_detail", pk=pk)
    return render(request, "dashboard/schedule_desludging.html", {"service_request": service_request})


@login_required
@role_required("ADMIN")
def admin_schedule_by_barangay(request):
    """Schedule view showing customers per barangay with 10-person limit tracking"""
    barangays = (
        Schedule.objects.values("barangay")
        .annotate(count=Count("id"))
        .order_by("barangay")
    )

    barangay_details = []
    for barangay in barangays:
        schedules = list(
            Schedule.objects.filter(barangay=barangay["barangay"])
            .select_related("service_request__consumer", "assigned_staff")[:10]
        )
        count = len(schedules)
        percentage = (count / 10) * 100 if count > 0 else 0
        barangay_details.append({
            "barangay": barangay["barangay"],
            "count": count,
            "percentage": percentage,
            "schedules": schedules,
            "is_full": count >= 10,
        })

    context = {"barangay_details": barangay_details, "active_tab": "schedule"}
    return render(request, "dashboard/admin_schedule.html", context)


@login_required
@role_required("ADMIN")
def admin_membership(request):
    """Admin membership view with sub-tabs"""
    tab = request.GET.get("tab", "registration")

    if tab == "registration":
        consumers = User.objects.filter(role=User.Role.CONSUMER, is_approved=False)
    elif tab == "account_management":
        consumers = User.objects.filter(role=User.Role.CONSUMER).select_related("consumer_profile")
    elif tab == "service_history":
        consumers = User.objects.filter(role=User.Role.CONSUMER).select_related("consumer_profile")
    else:
        consumers = User.objects.filter(role=User.Role.CONSUMER)

    context = {"consumers": consumers, "active_tab": tab}
    return render(request, "dashboard/admin_membership.html", context)


@login_required
@role_required("ADMIN")
def member_service_history(request, user_id):
    """Service history for a specific member"""
    consumer = get_object_or_404(User, pk=user_id, role=User.Role.CONSUMER)
    service_requests = ServiceRequest.objects.filter(
        consumer=consumer,
        status=ServiceRequest.Status.COMPLETED,
    ).order_by("-request_date")

    four_years_ago = date.today() - timedelta(days=4 * 365)
    recent_services = service_requests.filter(request_date__gte=four_years_ago)
    total_cubic_meters = sum(
        getattr(req, "cubic_meters", 0) or 0 for req in recent_services
    )
    remaining_balance = max(0, 5 - total_cubic_meters)

    context = {
        "consumer": consumer,
        "service_requests": service_requests,
        "remaining_balance": remaining_balance,
        "total_used": total_cubic_meters,
    }
    return render(request, "dashboard/member_service_history.html", context)


@login_required
@role_required("ADMIN")
def admin_computation(request):
    """Cost computation and receipt generation"""
    from dashboard.forms import QuickComputationForm

    form = QuickComputationForm()
    computation_result = None

    if request.method == "POST":
        form = QuickComputationForm(request.POST)
        if form.is_valid():
            category = form.cleaned_data["category"]
            location = form.cleaned_data["location"]
            cubic_meters = form.cleaned_data["cubic_meters"]
            distance = form.cleaned_data.get("distance_km", Decimal("0")) or Decimal("0")
            meals_transport = form.cleaned_data.get("meals_transport", Decimal("0")) or Decimal("0")

            from dashboard.models import ConfigurableRate
            R = ConfigurableRate.get

            desludging_fee = max(cubic_meters, R("min_cubic_meters")) * R("desludging_per_m3")
            inspection_fee = R("inspection_fee")

            if location == "inside":
                if category == "RESIDENTIAL":
                    fixed_trucking = R("residential_trucking_within")
                else:
                    fixed_trucking = R("commercial_trucking_within")
                distance_cost = Decimal("0")
                wear_tear = Decimal("0")
            else:
                fixed_trucking = R("outside_trucking")
                excess = max(Decimal("0"), distance - R("free_km"))
                distance_cost = excess * R("per_km_rate") * 2
                base_for_wear = fixed_trucking + distance_cost + desludging_fee
                wear_tear = base_for_wear * R("wear_tear_pct") / Decimal("100")

            if not meals_transport:
                meals_transport = 4 * R("meals_per_head")

            total = fixed_trucking + desludging_fee + distance_cost + wear_tear + meals_transport + inspection_fee

            computation_result = {
                "category": category,
                "location": location,
                "cubic_meters": cubic_meters,
                "distance": distance,
                "fixed_trucking": fixed_trucking,
                "desludging_fee": desludging_fee,
                "inspection_fee": inspection_fee,
                "distance_cost": distance_cost,
                "wear_tear": wear_tear,
                "meals_transport": meals_transport,
                "total": total,
                "prepared_by": request.user.get_full_name(),
            }

    context = {"form": form, "computation_result": computation_result}
    return render(request, "dashboard/admin_computation.html", context)


@login_required
@role_required("ADMIN")
def generate_receipt(request):
    """Generate printable receipt"""
    return redirect("dashboard:admin_computation")


@login_required
@role_required("ADMIN")
def admin_declogging_app(request):
    """Declogging services application form"""
    if request.method == "POST":
        messages.success(request, "Application form generated successfully.")
        return redirect("dashboard:admin_declogging_app")
    return render(request, "dashboard/admin_declogging_app.html")
