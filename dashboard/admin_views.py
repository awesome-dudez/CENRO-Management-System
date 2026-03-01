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


def _get_dashboard_context(request):
    """Shared context for admin dashboard and analytics pages."""
    import json
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

    return {
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


@login_required
@role_required("ADMIN")
def admin_dashboard(request):
    """Main admin dashboard with charts and statistics"""
    context = _get_dashboard_context(request)
    return render(request, "dashboard/admin_dashboard.html", context)


def _get_analytics_payload(request=None):
    """
    Build analytics payload for API and initial page load.
    Uses real ServiceRequest/User data where possible; dummy data for visitor-style metrics.
    Structure is ready to plug in backend metrics (e.g. page views, browsers) later.
    """
    today = date.today()
    thirty_days_ago = today - timedelta(days=30)

    # Real: total requests in last 30 days (as "Total Visitors" analogue)
    total_requests_30 = ServiceRequest.objects.filter(
        created_at__date__gte=thirty_days_ago
    ).count()
    prev_30 = today - timedelta(days=60)
    prev_count = ServiceRequest.objects.filter(
        created_at__date__gte=prev_30,
        created_at__date__lt=thirty_days_ago,
    ).count()
    trend_pct = (
        round((total_requests_30 - prev_count) / prev_count * 100, 1)
        if prev_count else 0
    )

    # New vs Returning: incoming vs completed per month (last 3 months, oldest first)
    months = []
    new_vals = []
    returning_vals = []
    for i in range(2, -1, -1):
        month_start = today.replace(day=1)
        for _ in range(i):
            month_start = (month_start - timedelta(days=1)).replace(day=1)
        month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        months.append(month_start.strftime("%b"))
        new_vals.append(
            ServiceRequest.objects.filter(
                created_at__date__gte=month_start,
                created_at__date__lte=month_end,
            ).count()
        )
        returning_vals.append(
            ServiceRequest.objects.filter(
                status=ServiceRequest.Status.COMPLETED,
                request_date__gte=month_start,
                request_date__lte=month_end,
            ).count()
        )
    if not months:
        months, new_vals, returning_vals = ["Jan", "Feb", "Mar"], [12, 19, 8], [10, 14, 6]

    # Top sources: real – top barangays by request count
    top_barangays = (
        ServiceRequest.objects.values("barangay")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )
    top_sources_labels = [b["barangay"] or "Unknown" for b in top_barangays]
    top_sources_values = [b["count"] for b in top_barangays]
    if not top_sources_labels:
        top_sources_labels = ["Poblacion", "Magatas", "San Isidro", "Naraja", "Balabag"]
        top_sources_values = [45, 32, 28, 22, 18]

    # Line chart: request trend by week (last 7 weeks)
    line_labels = []
    line_unique = []
    line_site = []
    line_views = []
    for i in range(6, -1, -1):
        week_end = today - timedelta(weeks=i)
        week_start = week_end - timedelta(days=6)
        line_labels.append(week_start.strftime("%m/%d"))
        incoming = ServiceRequest.objects.filter(
            created_at__date__gte=week_start,
            created_at__date__lte=week_end,
        ).count()
        completed = ServiceRequest.objects.filter(
            status=ServiceRequest.Status.COMPLETED,
            request_date__gte=week_start,
            request_date__lte=week_end,
        ).count()
        line_unique.append(incoming)
        line_site.append(completed)
        line_views.append(incoming + completed)

    # Donut: service type distribution (real)
    by_type = (
        ServiceRequest.objects.values("service_type")
        .annotate(count=Count("id"))
    )
    donut_labels = []
    donut_values = []
    for t in by_type:
        st = t["service_type"]
        donut_labels.append(st.replace("_", " ").title() if st else "Other")
        donut_values.append(t["count"])
    if not donut_labels:
        donut_labels = ["Residential", "Commercial", "Grass Cutting"]
        donut_values = [48, 23, 15]

    # Table: top "pages" = top barangays with view count (real)
    top_pages = [
        {"name": label, "views": val}
        for label, val in zip(top_sources_labels, top_sources_values)
    ]
    if not top_pages:
        top_pages = [
            {"name": "Home", "views": 110},
            {"name": "Services", "views": 39},
            {"name": "About Us", "views": 32},
            {"name": "Our Team", "views": 29},
            {"name": "Contact Us", "views": 18},
        ]

    return {
        "kpi": {
            "total_visitors_30d": total_requests_30,
            "trend_pct": trend_pct,
        },
        "new_vs_returning": {
            "labels": months,
            "new": new_vals,
            "returning": returning_vals,
        },
        "top_sources": {
            "labels": top_sources_labels,
            "values": top_sources_values,
        },
        "visitor_overview": {
            "labels": line_labels,
            "unique": line_unique,
            "site": line_site,
            "pageViews": line_views,
        },
        "top_browsers": {
            "labels": donut_labels,
            "values": donut_values,
        },
        "top_pages": top_pages,
    }


@login_required
@role_required("ADMIN")
def admin_map_requests(request):
    """Map view: list of consumers with completed requests (with GPS); click consumer to show their locations on map."""
    import json
    qs = (
        ServiceRequest.objects.filter(
            status=ServiceRequest.Status.COMPLETED,
            gps_latitude__isnull=False,
            gps_longitude__isnull=False,
        )
        .select_related("consumer")
        .order_by("consumer__first_name", "consumer__last_name", "-request_date")
    )
    consumers_seen = set()
    consumers = []
    requests_list = []
    for sr in qs:
        try:
            lat = float(sr.gps_latitude)
            lng = float(sr.gps_longitude)
        except (TypeError, ValueError):
            continue
        cid = sr.consumer_id
        cname = (sr.consumer.get_full_name() or sr.consumer.username or "").strip() or "Unknown"
        if cid not in consumers_seen:
            consumers_seen.add(cid)
            consumers.append({"id": cid, "name": cname})
        date_completed = sr.request_date.strftime("%b %d, %Y") if sr.request_date else ""
        requests_list.append({
            "consumer_id": cid,
            "consumer_name": cname,
            "client_name": sr.client_name or "",
            "service_type": sr.get_service_type_display() if sr.service_type else "",
            "address": sr.address or "",
            "date_completed": date_completed,
            "lat": lat,
            "lng": lng,
        })
    consumers_json = json.dumps(consumers)
    requests_json = json.dumps(requests_list)
    return render(
        request,
        "dashboard/admin_map_requests.html",
        {
            "consumers": consumers,
            "consumers_json": consumers_json,
            "requests_json": requests_json,
        },
    )


@login_required
@role_required("ADMIN")
def admin_analytics(request):
    """Analytics page – KPI cards, charts, table; initial data for first paint."""
    payload = _get_analytics_payload(request)
    return render(request, "dashboard/admin_analytics.html", {"analytics_data": payload})


@login_required
@role_required("ADMIN")
def analytics_api(request):
    """JSON endpoint for real-time analytics (polling)."""
    payload = _get_analytics_payload(request)
    return JsonResponse(payload)


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
def confirm_payment(request, pk):
    """Admin approves a customer's payment after verifying the uploaded receipt."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if request.method == "POST":
        if not service_request.treasurer_receipt:
            messages.error(request, "No payment receipt uploaded for this request.")
            return redirect("services:request_detail", pk=pk)

        service_request.status = ServiceRequest.Status.PAID
        service_request.payment_confirmed_at = timezone.now()
        service_request.save(update_fields=["status", "payment_confirmed_at"])

        # Update computation payment status to PAID (unless it's a free BAWAD service)
        computation = getattr(service_request, "computation", None)
        if computation:
            from dashboard.models import ServiceComputation

            if computation.payment_status != ServiceComputation.PaymentStatus.FREE:
                # Avoid triggering recalculation logic; direct update is enough.
                ServiceComputation.objects.filter(pk=computation.pk).update(
                    payment_status=ServiceComputation.PaymentStatus.PAID
                )

        messages.success(request, "Payment confirmed. Request marked as Paid.")
        return redirect("services:request_detail", pk=pk)

    return redirect("services:request_detail", pk=pk)

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
    """Admin membership view with sub-tabs and search."""
    tab = request.GET.get("tab", "registration")
    search = (request.GET.get("q") or "").strip()

    if tab == "registration":
        consumers = User.objects.filter(role=User.Role.CONSUMER, is_approved=False)
    elif tab == "account_management":
        consumers = User.objects.filter(role=User.Role.CONSUMER).select_related("consumer_profile")
    elif tab == "service_history":
        consumers = User.objects.filter(role=User.Role.CONSUMER).select_related("consumer_profile")
    else:
        consumers = User.objects.filter(role=User.Role.CONSUMER)

    if search and tab in ("account_management", "service_history"):
        consumers = consumers.filter(
            Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(username__icontains=search)
            | Q(email__icontains=search)
            | Q(consumer_profile__barangay__icontains=search)
            | Q(consumer_profile__street_address__icontains=search)
            | Q(consumer_profile__municipality__icontains=search)
            | Q(consumer_profile__mobile_number__icontains=search)
        )

    context = {"consumers": consumers, "active_tab": tab, "search_query": search}
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
            import math
            R = ConfigurableRate.get

            # Desludging fee: align with main computation rules
            max_m3_per_trip = Decimal("5")
            effective_m3 = max(cubic_meters, R("min_cubic_meters"))
            desludging_per_m3 = R("desludging_per_m3")
            second_trip_surcharge = R("second_trip_surcharge")

            trips = max(1, math.ceil(float(effective_m3) / float(max_m3_per_trip)))
            desludging_fee = Decimal("0")
            desludging_breakdown = []
            for t in range(trips):
                # Trip 1: always 5 m³ at base rate; Trip 2+ use remaining volume at (base + surcharge)
                if t == 0:
                    vol_this_trip = max_m3_per_trip
                else:
                    remaining = effective_m3 - (t * max_m3_per_trip)
                    vol_this_trip = min(max_m3_per_trip, max(Decimal("0"), remaining))
                rate = desludging_per_m3 if t == 0 else (desludging_per_m3 + second_trip_surcharge)
                amount = vol_this_trip * rate
                desludging_fee += amount

                if t == 0:
                    label = f"Trip 1: 5 m³ × ₱{desludging_per_m3}/m³"
                else:
                    label = (
                        f"Trip {t + 1}: {vol_this_trip} m³ × ₱{desludging_per_m3 + second_trip_surcharge}/m³ "
                        f"(₱{desludging_per_m3} + ₱{second_trip_surcharge} surcharge)"
                    )
                desludging_breakdown.append({"label": label, "amount": amount})

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
                # Wear & tear: 20% of (trucking + distance cost) only
                base_for_wear = fixed_trucking + distance_cost
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
                "desludging_breakdown": desludging_breakdown,
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
