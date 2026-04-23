from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.text import slugify
from django.utils import timezone
from django.views.decorators.http import require_POST
from urllib.parse import urlencode

from accounts.constants import CONSUMER_DEFAULT_RESET_PASSWORD
from accounts.decorators import role_required
from accounts.models import ConsumerProfile, User
from services.models import ServiceEquipment, ServiceRequest, Notification
from scheduling.models import Schedule


def _membership_consumer_qs():
    """
    Consumers shown under Members / Account Management only.
    Excludes Django staff and superusers so admin/staff accounts never appear here,
    even if role was left as CONSUMER.
    """
    return User.objects.filter(role=User.Role.CONSUMER).exclude(is_superuser=True).exclude(is_staff=True)


def _get_dashboard_context(request):
    """Shared context for admin dashboard and analytics pages."""
    import json
    today = date.today()
    start_of_month = today.replace(day=1)

    # Keep the admin workflow clean: expire stale requests and send 4-day warnings.
    try:
        ServiceRequest.expire_stale_requests()
    except Exception:
        pass

    total_requests = ServiceRequest.objects.count()
    # All requests still in the workflow (not finished, cancelled, or expired).
    pending_count = ServiceRequest.objects.exclude(
        status__in=[
            ServiceRequest.Status.COMPLETED,
            ServiceRequest.Status.CANCELLED,
            ServiceRequest.Status.EXPIRED,
        ]
    ).count()
    completed_this_month = ServiceRequest.objects.filter(
        status=ServiceRequest.Status.COMPLETED,
        updated_at__date__gte=start_of_month,
    ).count()

    last_month_start = (start_of_month - timedelta(days=1)).replace(day=1)
    last_month_completed = ServiceRequest.objects.filter(
        status=ServiceRequest.Status.COMPLETED,
        updated_at__date__gte=last_month_start,
        updated_at__date__lt=start_of_month,
    ).count()

    efficiency_change = 0
    if last_month_completed > 0:
        efficiency_change = ((completed_this_month - last_month_completed) / last_month_completed) * 100

    # Rolling last four calendar weeks (ending today) for trends on analytics.
    weeks_data = []
    for i in range(4):
        weeks_ago = 3 - i
        week_end = today - timedelta(days=7 * weeks_ago)
        week_start = week_end - timedelta(days=6)
        label = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d')}"
        incoming = ServiceRequest.objects.filter(
            created_at__date__gte=week_start,
            created_at__date__lte=week_end,
        ).count()
        completed = ServiceRequest.objects.filter(
            status=ServiceRequest.Status.COMPLETED,
            updated_at__date__gte=week_start,
            updated_at__date__lte=week_end,
        ).count()
        weeks_data.append({"week": label, "incoming": incoming, "completed": completed})
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

    equipment_active_count = ServiceEquipment.objects.filter(is_active=True).count()
    equipment_inactive_count = ServiceEquipment.objects.filter(is_active=False).count()
    equipment_total_count = equipment_active_count + equipment_inactive_count

    consumer_accounts_count = (
        User.objects.filter(role=User.Role.CONSUMER, is_active=True)
        .exclude(is_superuser=True)
        .count()
    )
    staff_active_count = User.objects.filter(
        role=User.Role.STAFF,
        is_active=True,
        is_approved=True,
    ).count()
    upcoming_schedules_count = Schedule.objects.filter(service_date__gte=today).count()
    last_request = ServiceRequest.objects.order_by("-created_at").only("created_at").first()

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
        "equipment_active_count": equipment_active_count,
        "equipment_inactive_count": equipment_inactive_count,
        "equipment_total_count": equipment_total_count,
        "consumer_accounts_count": consumer_accounts_count,
        "staff_active_count": staff_active_count,
        "upcoming_schedules_count": upcoming_schedules_count,
        "last_request_at": last_request.created_at if last_request else None,
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

    desludging_types = [
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ]

    def _build_split_payload(service_filter: Q):
        split_total_30 = ServiceRequest.objects.filter(
            service_filter,
            created_at__date__gte=thirty_days_ago,
        ).count()
        split_prev_30 = ServiceRequest.objects.filter(
            service_filter,
            created_at__date__gte=prev_30,
            created_at__date__lt=thirty_days_ago,
        ).count()
        split_trend = (
            round((split_total_30 - split_prev_30) / split_prev_30 * 100, 1)
            if split_prev_30 else 0
        )

        split_new_vals = []
        split_returning_vals = []
        for i in range(2, -1, -1):
            month_start = today.replace(day=1)
            for _ in range(i):
                month_start = (month_start - timedelta(days=1)).replace(day=1)
            month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            split_new_vals.append(
                ServiceRequest.objects.filter(
                    service_filter,
                    created_at__date__gte=month_start,
                    created_at__date__lte=month_end,
                ).count()
            )
            split_returning_vals.append(
                ServiceRequest.objects.filter(
                    service_filter,
                    status=ServiceRequest.Status.COMPLETED,
                    request_date__gte=month_start,
                    request_date__lte=month_end,
                ).count()
            )

        split_top_barangays = (
            ServiceRequest.objects.filter(service_filter)
            .values("barangay")
            .annotate(count=Count("id"))
            .order_by("-count")[:5]
        )
        top_label = "No data"
        top_count = 0
        if split_top_barangays:
            top_label = split_top_barangays[0]["barangay"] or "Unknown"
            top_count = split_top_barangays[0]["count"]

        split_sources_qs = (
            ServiceRequest.objects.filter(service_filter)
            .values("barangay")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )
        split_top_sources_labels = [b["barangay"] or "Unknown" for b in split_sources_qs]
        split_top_sources_values = [b["count"] for b in split_sources_qs]
        if not split_top_sources_labels:
            split_top_sources_labels = ["No data"]
            split_top_sources_values = [0]

        split_line_labels = []
        split_line_unique = []
        split_line_site = []
        split_line_views = []
        for i in range(6, -1, -1):
            week_end = today - timedelta(weeks=i)
            week_start = week_end - timedelta(days=6)
            split_line_labels.append(week_start.strftime("%m/%d"))
            incoming = ServiceRequest.objects.filter(
                service_filter,
                created_at__date__gte=week_start,
                created_at__date__lte=week_end,
            ).count()
            completed = ServiceRequest.objects.filter(
                service_filter,
                status=ServiceRequest.Status.COMPLETED,
                request_date__gte=week_start,
                request_date__lte=week_end,
            ).count()
            split_line_unique.append(incoming)
            split_line_site.append(completed)
            split_line_views.append(incoming + completed)

        status_qs = (
            ServiceRequest.objects.filter(service_filter)
            .values("status")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        split_status_labels = [s["status"].replace("_", " ").title() if s["status"] else "Unknown" for s in status_qs]
        split_status_values = [s["count"] for s in status_qs]
        if not split_status_labels:
            split_status_labels = ["No data"]
            split_status_values = [0]

        return {
            "kpi_30d": split_total_30,
            "trend_pct": split_trend,
            "new_vs_returning": {
                "labels": months,
                "new": split_new_vals,
                "returning": split_returning_vals,
            },
            "top_sources": {
                "labels": split_top_sources_labels,
                "values": split_top_sources_values,
            },
            "visitor_overview": {
                "labels": split_line_labels,
                "unique": split_line_unique,
                "site": split_line_site,
                "pageViews": split_line_views,
            },
            "top_browsers": {
                "labels": split_status_labels,
                "values": split_status_values,
            },
            "top_pages": [
                {"name": label, "views": val}
                for label, val in zip(split_top_sources_labels, split_top_sources_values)
            ],
            "top_barangay": {
                "label": top_label,
                "count": top_count,
            },
        }

    split_analytics = {
        "desludging": _build_split_payload(Q(service_type__in=desludging_types)),
        "grass_cutting": _build_split_payload(Q(service_type=ServiceRequest.ServiceType.GRASS_CUTTING)),
    }

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
        "split": split_analytics,
        "top_pages": top_pages,
    }


def _map_pin_color_for_status(status: str) -> str:
    """Stable hex colors for Leaflet markers by request status."""
    colors = {
        ServiceRequest.Status.SUBMITTED: "#3498db",
        ServiceRequest.Status.INSPECTION_FEE_DUE: "#e67e22",
        ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION: "#ca6f1e",
        ServiceRequest.Status.EXPIRED: "#7f8c8d",
        ServiceRequest.Status.UNDER_REVIEW: "#2980b9",
        ServiceRequest.Status.INSPECTION_SCHEDULED: "#9b59b6",
        ServiceRequest.Status.INSPECTED: "#8e44ad",
        ServiceRequest.Status.COMPUTATION_SENT: "#1abc9c",
        ServiceRequest.Status.AWAITING_PAYMENT: "#f39c12",
        ServiceRequest.Status.PAID: "#16a085",
        ServiceRequest.Status.DESLUDGING_SCHEDULED: "#2ecc71",
        ServiceRequest.Status.COMPLETED: "#1e8449",
        ServiceRequest.Status.GRASS_PENDING_PAYMENT: "#e74c3c",
        ServiceRequest.Status.GRASS_PAYMENT_AWAITING_VERIFICATION: "#c0392b",
        ServiceRequest.Status.CANCELLED: "#95a5a6",
    }
    return colors.get(status, "#34495e")


@login_required
@role_required("ADMIN")
def admin_map_requests(request):
    """Map of all requests with GPS: clustered markers, colors by status, filter by status."""
    import json

    qs = (
        ServiceRequest.objects.filter(
            gps_latitude__isnull=False,
            gps_longitude__isnull=False,
        )
        .select_related("consumer")
        .order_by("-updated_at")
    )
    requests_list = []
    statuses_present: set[str] = set()
    for sr in qs:
        try:
            lat = float(sr.gps_latitude)
            lng = float(sr.gps_longitude)
        except (TypeError, ValueError):
            continue
        cid = sr.consumer_id
        cname = (sr.consumer.get_full_name() or sr.consumer.username or "").strip() or "Unknown"
        date_str = sr.request_date.strftime("%b %d, %Y") if sr.request_date else ""
        st = sr.status
        statuses_present.add(st)
        requests_list.append(
            {
                "request_id": sr.pk,
                "consumer_id": cid,
                "consumer_name": cname,
                "client_name": sr.client_name or "",
                "service_type": sr.get_service_type_display() if sr.service_type else "",
                "address": sr.address or "",
                "request_date": date_str,
                "status": st,
                "status_label": sr.get_status_display(),
                "pin_color": _map_pin_color_for_status(st),
                "detail_url": reverse("services:request_detail", args=[sr.pk]),
                "lat": lat,
                "lng": lng,
            }
        )

    status_filters = []
    for code, label in ServiceRequest.Status.choices:
        if code in statuses_present:
            status_filters.append(
                {
                    "value": code,
                    "label": label,
                    "color": _map_pin_color_for_status(code),
                }
            )

    return render(
        request,
        "dashboard/admin_map_requests.html",
        {
            "map_request_count": len(requests_list),
            "status_filters": status_filters,
            "requests_json": json.dumps(requests_list),
        },
    )


@login_required
@role_required("ADMIN")
def admin_analytics(request):
    """Analytics page – KPI cards, charts, table; initial data for first paint."""
    context = _get_dashboard_context(request)
    context["analytics_data"] = _get_analytics_payload(request)
    return render(request, "dashboard/admin_analytics.html", context)


@login_required
@role_required("ADMIN")
def analytics_api(request):
    """JSON endpoint for real-time analytics (polling)."""
    payload = _get_analytics_payload(request)
    return JsonResponse(payload)


@login_required
@role_required("ADMIN", "STAFF")
def admin_requests(request):
    """Admin requests view with sub-tabs"""
    # Cleanup before we build the Pending tab list.
    try:
        ServiceRequest.expire_stale_requests()
    except Exception:
        pass

    tab = request.GET.get("tab")
    if not tab:
        # For staff/inspectors, default to the Inspection tab since
        # that's the only workflow they use. Admins still default to Pending.
        if getattr(request.user, "role", None) == User.Role.STAFF:
            tab = "inspection"
        else:
            tab = "pending"
    # New: request-type filter controlled from UI selector.
    # "grass" -> Grass Cutting only; "declogging" -> all desludging/declogging requests;
    # anything else (including empty) -> all service types.
    request_type = request.GET.get("request_type") or "all"
    sort = request.GET.get("sort", "date")
    direction = request.GET.get("dir", "desc")

    # Admins can see all requests; staff only see requests assigned to them.
    if getattr(request.user, "role", None) == User.Role.STAFF:
        base_qs = ServiceRequest.objects.filter(assigned_inspector=request.user)
    else:
        base_qs = ServiceRequest.objects.all()
    if request_type == "grass":
        base_qs = base_qs.filter(service_type=ServiceRequest.ServiceType.GRASS_CUTTING)
    elif request_type == "declogging":
        # Group both residential and commercial desludging under "Declogging"
        base_qs = base_qs.filter(
            service_type__in=[
                ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
                ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
            ]
        )

    if tab == "pending":
        requests_qs = base_qs.filter(
            status__in=[
                ServiceRequest.Status.SUBMITTED,
                ServiceRequest.Status.UNDER_REVIEW,
                ServiceRequest.Status.INSPECTION_FEE_DUE,
                ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION,
                ServiceRequest.Status.GRASS_PENDING_PAYMENT,
                ServiceRequest.Status.GRASS_PAYMENT_AWAITING_VERIFICATION,
            ]
        )
    elif tab == "inspection":
        requests_qs = base_qs.filter(
            status__in=[
                ServiceRequest.Status.INSPECTION_SCHEDULED,
                ServiceRequest.Status.INSPECTED,
            ]
        )
    elif tab == "computation":
        requests_qs = base_qs.filter(
            status__in=[
                ServiceRequest.Status.COMPUTATION_SENT,
                ServiceRequest.Status.AWAITING_PAYMENT,
            ]
        )
    elif tab == "schedule":
        requests_qs = base_qs.filter(
            status__in=[
                ServiceRequest.Status.PAID,
                ServiceRequest.Status.DESLUDGING_SCHEDULED,
            ]
        )
    elif tab == "completed":
        requests_qs = base_qs.filter(
            status__in=[
                ServiceRequest.Status.COMPLETED,
                ServiceRequest.Status.CANCELLED,
            ],
        )
    elif tab == "all":
        requests_qs = base_qs
    elif tab == "open":
        # Matches dashboard "open requests" count: in workflow, excluding terminal states.
        requests_qs = base_qs.exclude(
            status__in=[
                ServiceRequest.Status.COMPLETED,
                ServiceRequest.Status.CANCELLED,
                ServiceRequest.Status.EXPIRED,
            ]
        )
    else:
        requests_qs = base_qs

    # Apply sorting (ID, Barangay, or Date) across all tabs
    sort_field_map = {
        "id": "id",
        "barangay": "barangay",
        "date": "request_date",
    }
    sort_field = sort_field_map.get(sort, "request_date")
    prefix = "-" if direction == "desc" else ""
    requests_qs = requests_qs.order_by(f"{prefix}{sort_field}")

    context = {
        "requests": requests_qs,
        "active_tab": tab,
        "request_type": request_type,
        "sort": sort,
        "direction": direction,
        "request_type_options": [
            {"key": "grass", "label": "Grass Cutting"},
            {"key": "declogging", "label": "Declogging"},
        ],
    }
    return render(request, "dashboard/admin_requests.html", context)


@login_required
@role_required("ADMIN")
def confirm_payment(request, pk):
    """Admin approves a customer's payment after verifying the uploaded receipt."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.service_type == ServiceRequest.ServiceType.GRASS_CUTTING:
        messages.error(
            request,
            "Grass Cutting uses a separate verification step. Open the request and use Confirm Grass Cutting Request.",
        )
        return redirect("services:request_detail", pk=pk)

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
                ServiceComputation.objects.filter(pk=computation.pk).update(
                    payment_status=ServiceComputation.PaymentStatus.PAID
                )

        Notification.objects.create(
            user=service_request.consumer,
            message=(
                f"Payment for request #{service_request.id} has been confirmed. "
                "Your request is now marked as Paid and desludging will be scheduled soon."
            ),
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )
        if (
            service_request.requested_by
            and service_request.requested_by != service_request.consumer
        ):
            Notification.objects.create(
                user=service_request.requested_by,
                message=(
                    f"Payment for request #{service_request.id} "
                    f"({service_request.client_name}) has been confirmed."
                ),
                notification_type=Notification.NotificationType.STATUS_CHANGE,
                related_request=service_request,
            )

        messages.success(request, "Payment confirmed. Request marked as Paid.")
        return redirect("services:request_detail", pk=pk)

    return redirect("services:request_detail", pk=pk)


@login_required
@role_required("ADMIN")
@require_POST
def confirm_grass_request(request, pk):
    """After treasurer payment receipt is uploaded, admin confirms grass cutting (marks completed)."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING:
        messages.error(request, "This action applies only to Grass Cutting requests.")
        return redirect("services:request_detail", pk=pk)
    if service_request.status != ServiceRequest.Status.GRASS_PAYMENT_AWAITING_VERIFICATION:
        messages.error(request, "This request is not awaiting payment verification.")
        return redirect("services:request_detail", pk=pk)
    if not service_request.treasurer_receipt:
        messages.error(request, "No payment receipt uploaded for this request.")
        return redirect("services:request_detail", pk=pk)

    service_request.status = ServiceRequest.Status.COMPLETED
    service_request.payment_confirmed_at = timezone.now()
    service_request.save(update_fields=["status", "payment_confirmed_at", "updated_at"])

    Notification.objects.create(
        user=service_request.consumer,
        message=(
            f"Your Grass Cutting request #{service_request.id} has been verified. "
            "Payment is confirmed and your service may proceed per the agreed schedule."
        ),
        notification_type=Notification.NotificationType.STATUS_CHANGE,
        related_request=service_request,
    )
    if service_request.requested_by_id and service_request.requested_by_id != service_request.consumer_id:
        Notification.objects.create(
            user_id=service_request.requested_by_id,
            message=(
                f"Grass Cutting request #{service_request.id} that you submitted for "
                f"{service_request.client_name} has been verified and confirmed."
            ),
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )
    messages.success(request, "Grass Cutting request confirmed and marked as completed.")
    return redirect("services:request_detail", pk=pk)


@login_required
@role_required("ADMIN")
@require_POST
def cancel_grass_request(request, pk):
    """Admin cancels a grass cutting request (before or after payment upload)."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING:
        messages.error(request, "This action applies only to Grass Cutting requests.")
        return redirect("services:request_detail", pk=pk)
    if service_request.status not in (
        ServiceRequest.Status.GRASS_PENDING_PAYMENT,
        ServiceRequest.Status.GRASS_PAYMENT_AWAITING_VERIFICATION,
    ):
        messages.error(request, "This Grass Cutting request cannot be cancelled from its current status.")
        return redirect("services:request_detail", pk=pk)

    service_request.status = ServiceRequest.Status.CANCELLED
    service_request.save(update_fields=["status", "updated_at"])

    Notification.objects.create(
        user=service_request.consumer,
        message=(
            f"Your Grass Cutting request #{service_request.id} has been cancelled by the office. "
            "If you have questions, please contact CENRO."
        ),
        notification_type=Notification.NotificationType.STATUS_CHANGE,
        related_request=service_request,
    )
    if service_request.requested_by_id and service_request.requested_by_id != service_request.consumer_id:
        Notification.objects.create(
            user_id=service_request.requested_by_id,
            message=(
                f"Grass Cutting request #{service_request.id} for {service_request.client_name} "
                "has been cancelled by the office."
            ),
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )
    messages.success(request, "Grass Cutting request has been cancelled.")
    return redirect("services:request_detail", pk=pk)


@login_required
@role_required("ADMIN")
def confirm_inspection_fee(request, pk):
    """Admin verifies the uploaded inspection fee receipt and unlocks inspector assignment."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if request.method == "POST":
        if not service_request.inspection_fee_receipt:
            messages.error(request, "No inspection fee receipt uploaded for this request.")
            return redirect("services:request_detail", pk=pk)

        service_request.inspection_fee_paid = True
        service_request.status = ServiceRequest.Status.UNDER_REVIEW
        service_request.save(update_fields=["inspection_fee_paid", "status"])

        Notification.objects.create(
            user=service_request.consumer,
            message=(
                f"Inspection fee for request #{service_request.id} has been verified. "
                "Your request is now under review and an inspector will be scheduled."
            ),
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )
        messages.success(request, "Inspection fee verified. Request is now under review.")
        return redirect("services:request_detail", pk=pk)

    return redirect("services:request_detail", pk=pk)


@login_required
@role_required("ADMIN")
@require_POST
def reject_inspection_fee(request, pk):
    """Decline the uploaded inspection fee receipt; customer must upload a new one."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.status != ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION:
        messages.error(request, "This request is not awaiting inspection fee verification.")
        return redirect("services:request_detail", pk=pk)
    if not service_request.inspection_fee_receipt:
        messages.error(request, "There is no inspection fee receipt to reject.")
        return redirect("services:request_detail", pk=pk)

    reason = (request.POST.get("reason") or "").strip()[:500]

    with transaction.atomic():
        if service_request.inspection_fee_receipt:
            service_request.inspection_fee_receipt.delete(save=False)
        service_request.inspection_fee_receipt = None
        service_request.inspection_fee_paid = False
        service_request.status = ServiceRequest.Status.INSPECTION_FEE_DUE
        new_notes = (service_request.notes or "").strip()
        if reason:
            new_notes = (new_notes + "\n[INSPECTION_FEE_REJECTED] " + reason).strip()
        service_request.notes = new_notes
        service_request.save(
            update_fields=["inspection_fee_receipt", "inspection_fee_paid", "status", "notes"]
        )

    msg = (
        f"The inspection fee receipt for request #{service_request.id} was not accepted. "
        "Please upload a clear photo or PDF of your official Treasurer receipt."
    )
    if reason:
        msg += f" Note from office: {reason}"

    Notification.objects.create(
        user=service_request.consumer,
        message=msg[:500],
        notification_type=Notification.NotificationType.STATUS_CHANGE,
        related_request=service_request,
    )
    if (
        service_request.requested_by_id
        and service_request.requested_by_id != service_request.consumer_id
    ):
        Notification.objects.create(
            user=service_request.requested_by,
            message=(
                f"Inspection fee receipt for request #{service_request.id} "
                f"({service_request.client_name}) was rejected; the account owner must re-upload."
            )[:500],
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )

    messages.success(request, "Inspection fee receipt rejected. The customer can upload a new receipt.")
    return redirect("services:request_detail", pk=pk)


@login_required
@role_required("ADMIN")
@require_POST
def waive_public_bayawan_inspection_fee(request, pk):
    """
    First-time inspection fee: waive for public-property desludging within Bayawan City
    (policy — computation is already ₱0 when the letter is saved).
    """
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not service_request.qualifies_public_bayawan_no_fees:
        messages.error(
            request,
            "This action applies only to residential or commercial desludging marked as public property "
            "with a location inside Bayawan City.",
        )
        return redirect("services:request_detail", pk=pk)
    if service_request.status not in (
        ServiceRequest.Status.INSPECTION_FEE_DUE,
        ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION,
    ):
        messages.error(
            request,
            "Waiving the inspection fee here is only available while the request is awaiting "
            "the ₱150 inspection fee payment or verification.",
        )
        return redirect("services:request_detail", pk=pk)

    with transaction.atomic():
        if service_request.inspection_fee_receipt:
            service_request.inspection_fee_receipt.delete(save=False)
            service_request.inspection_fee_receipt = None
        service_request.status = ServiceRequest.Status.UNDER_REVIEW
        service_request.save(update_fields=["inspection_fee_receipt", "status", "updated_at"])

    if not service_request.apply_public_bayawan_inspection_fee_waiver(
        notify_user=service_request.consumer,
    ):
        messages.error(request, "Could not record the inspection fee waiver.")
        return redirect("services:request_detail", pk=pk)

    if (
        service_request.requested_by_id
        and service_request.requested_by_id != service_request.consumer_id
    ):
        Notification.objects.create(
            user_id=service_request.requested_by_id,
            message=(
                f"Request #{service_request.id} ({service_request.client_name}): "
                "inspection fee waived — public property within Bayawan City."
            )[:500],
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )

    messages.success(
        request,
        "Inspection fee waived for this public Bayawan request. Computation charges remain ₱0 when the letter is prepared.",
    )
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
    # All staff accounts act as inspectors.
    inspectors_qs = User.objects.filter(role=User.Role.STAFF, is_approved=True).order_by(
        "first_name", "last_name"
    )
    # Extract any existing inspector + time info from notes for initial form & change comparison.
    existing_inspector_label = ""
    existing_time = ""
    if service_request.notes:
        marker = "Inspection scheduled with "
        idx = service_request.notes.rfind(marker)
        if idx != -1:
            segment = service_request.notes[idx + len(marker):]
            try:
                name_part, rest = segment.split(" on ", 1)
                existing_inspector_label = name_part.strip()
                if " at " in rest:
                    _, time_part = rest.split(" at ", 1)
                    existing_time = time_part.strip().rstrip(".")
            except ValueError:
                pass

    # Flag: has this request already had an inspection schedule before?
    had_prior_schedule = bool(
        service_request.inspection_date
        or existing_inspector_label
        or existing_time
    )

    if request.method == "POST":
        inspector_id = (request.POST.get("inspector_id") or "").strip()
        insp_date = request.POST.get("inspection_date") or ""
        insp_time = request.POST.get("inspection_time") or ""
        change_reason = (request.POST.get("change_reason") or "").strip()

        if not (inspector_id and insp_date and insp_time):
            messages.error(request, "Please select an inspector, date, and time.")
            context = {
                "service_request": service_request,
                "inspectors": inspectors_qs,
                "initial_inspector_id": inspector_id,
                "initial_inspector_label": existing_inspector_label,
                "had_prior_schedule": had_prior_schedule,
                "initial_date": insp_date or (service_request.inspection_date.isoformat() if service_request.inspection_date else ""),
                "initial_time": insp_time or existing_time,
                "change_reason": change_reason,
            }
            return render(request, "dashboard/assign_inspector.html", context)

        # Resolve inspector user
        try:
            inspector = inspectors_qs.get(pk=inspector_id)
        except User.DoesNotExist:
            messages.error(
                request,
                "The inspector you selected is not in the current assignable list (they may have been removed or deactivated). "
                "Refresh the page and choose an active inspector.",
            )
            context = {
                "service_request": service_request,
                "inspectors": inspectors_qs,
                "initial_inspector_id": "",
                "initial_inspector_label": existing_inspector_label,
                "had_prior_schedule": had_prior_schedule,
                "initial_date": insp_date,
                "initial_time": insp_time,
                "change_reason": change_reason,
            }
            return render(request, "dashboard/assign_inspector.html", context)

        # Ensure inspection fee has been paid (for first-time customers).
        if not service_request.inspection_fee_paid:
            messages.error(request, "Inspection fee must be verified before assigning an inspector.")
            return redirect("services:request_detail", pk=pk)

        # Determine if anything actually changed compared to prior schedule.
        old_date_str = service_request.inspection_date.isoformat() if service_request.inspection_date else ""
        changed = (
            inspector.pk != (service_request.assigned_inspector_id or 0)
            or insp_date != old_date_str
            or insp_time != (existing_time or "")
        )

        # Only force a change reason when modifying an existing schedule.
        if had_prior_schedule and changed and not change_reason:
            messages.error(request, "Please provide a reason for the changes to the inspection schedule.")
            context = {
                "service_request": service_request,
                "inspectors": inspectors_qs,
                "initial_inspector_id": inspector.pk,
                "initial_inspector_label": existing_inspector_label,
                "had_prior_schedule": had_prior_schedule,
                "initial_date": insp_date,
                "initial_time": insp_time,
                "change_reason": change_reason,
            }
            return render(request, "dashboard/assign_inspector.html", context)

        inspector_label = inspector.get_full_name() or inspector.username
        note_line = f"Inspection scheduled with {inspector_label} on {insp_date} at {insp_time}."
        if changed and change_reason:
            note_line += f" Reason: {change_reason}"

        existing_notes = service_request.notes or ""
        service_request.notes = (existing_notes + "\n" if existing_notes else "") + note_line
        service_request.assigned_inspector = inspector
        service_request.inspection_date = insp_date
        service_request.status = ServiceRequest.Status.INSPECTION_SCHEDULED
        service_request.save(update_fields=["notes", "assigned_inspector", "inspection_date", "status"])

        # Notify customer (again if changed) with updated schedule.
        Notification.objects.create(
            user=service_request.consumer,
            message=(
                f"Your inspection schedule has been updated. "
                f"Inspector: {inspector_label}, Date: {insp_date}, Time: {insp_time}."
            ),
            notification_type=Notification.NotificationType.INSPECTOR_ASSIGNED,
            related_request=service_request,
        )
        messages.success(request, "Inspector assignment and schedule have been saved, and the customer was notified.")
        return redirect("services:request_detail", pk=pk)

    context = {
        "service_request": service_request,
        "inspectors": inspectors_qs,
        "initial_inspector_id": service_request.assigned_inspector_id or "",
        "initial_inspector_label": existing_inspector_label,
        "had_prior_schedule": had_prior_schedule,
        "initial_date": service_request.inspection_date.isoformat() if service_request.inspection_date else "",
        "initial_time": existing_time,
        "change_reason": "",
    }
    return render(request, "dashboard/assign_inspector.html", context)


@login_required
@role_required("ADMIN")
def waive_inspection(request, pk):
    """
    Allow admin to waive the physical inspection for a request.
    This also removes the inspection fee from any existing computation.
    """
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if request.method == "POST":
        note_flag = "[NO_INSPECTION_FEE]"
        existing_notes = service_request.notes or ""
        if note_flag not in existing_notes:
            appended = (existing_notes + "\n" if existing_notes else "") + f"{note_flag} Inspection waived by admin."
            service_request.notes = appended
            save_fields = ["notes"]
            # Was inspection already scheduled? Move back to under review so workflow matches waiver.
            if service_request.status == ServiceRequest.Status.INSPECTION_SCHEDULED:
                service_request.status = ServiceRequest.Status.UNDER_REVIEW
                save_fields.append("status")
            service_request.save(update_fields=save_fields)

        # If a computation already exists, recalculate without inspection fee.
        computation = getattr(service_request, "computation", None)
        if computation:
            computation.save()

        Notification.objects.create(
            user=service_request.consumer,
            message=(
                f"The physical inspection for request #{service_request.id} has been "
                "waived by the admin. Any inspection fee has been removed."
            ),
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )

        messages.success(
            request,
            "Inspection has been waived for this request. Any inspection fee will be removed from the computation.",
        )
    return redirect("services:request_detail", pk=pk)


# Statuses allowed to start/edit draft computation when inspection is waived ([NO_INSPECTION_FEE]).
# Includes INSPECTION_SCHEDULED / INSPECTED for rows scheduled or inspected before waiver; new waivers
# from INSPECTION_SCHEDULED are moved to UNDER_REVIEW in waive_inspection().
_WAIVED_COMPUTATION_ELIGIBLE_STATUSES = (
    ServiceRequest.Status.UNDER_REVIEW,
    ServiceRequest.Status.SUBMITTED,
    ServiceRequest.Status.INSPECTION_SCHEDULED,
    ServiceRequest.Status.INSPECTED,
)

# Broader than _WAIVED_COMPUTATION_ELIGIBLE_STATUSES: crew can be set or corrected after
# computation is created/sent (e.g. backfill personnel_count).
_WAIVED_CREW_ASSIGNMENT_STATUSES = (
    *_WAIVED_COMPUTATION_ELIGIBLE_STATUSES,
    ServiceRequest.Status.COMPUTATION_SENT,
    ServiceRequest.Status.AWAITING_PAYMENT,
    ServiceRequest.Status.PAID,
    ServiceRequest.Status.DESLUDGING_SCHEDULED,
)


def _try_create_initial_computation_for_waived_request(service_request, prepared_by_user):
    """
    When inspection is waived ([NO_INSPECTION_FEE]), create a draft ServiceComputation
    for desludging requests. Caller must ensure waived crew (driver/helpers) are assigned first.

    Returns:
        "created" — new draft computation was saved
        "exists" — computation already present
        "ineligible" — wrong type, status, or missing waiver flag
    """
    from dashboard.models import ServiceComputation
    from services.location import distance_from_cenro

    if service_request.service_type not in (
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ):
        return "ineligible"
    if "[NO_INSPECTION_FEE]" not in (service_request.notes or ""):
        return "ineligible"
    if service_request.status not in _WAIVED_COMPUTATION_ELIGIBLE_STATUSES:
        return "ineligible"
    if getattr(service_request, "computation", None):
        return "exists"

    is_outside = not service_request.is_within_bayawan
    dist = Decimal("0")
    if service_request.gps_latitude and service_request.gps_longitude:
        km = distance_from_cenro(
            float(service_request.gps_latitude),
            float(service_request.gps_longitude),
        )
        dist = Decimal(str(round(km, 2)))
    personnel_count = max(1, service_request.waived_inspection_personnel_count)
    computation = ServiceComputation.objects.create(
        service_request=service_request,
        is_outside_bayawan=is_outside,
        cubic_meters=service_request.cubic_meters or Decimal("5"),
        distance_km=dist,
        trips=1,
        personnel_count=personnel_count,
        prepared_by=prepared_by_user,
    )
    computation.is_finalized = False
    computation.save()
    service_request.fee_amount = computation.total_charge
    service_request.save(update_fields=["fee_amount"])
    return "created"


@login_required
@role_required("ADMIN")
def proceed_to_computation(request, pk):
    """
    When inspection has been waived, create an initial computation (if none exists)
    and send the admin to edit/finalize it. Allows the request to move to the next step.
    """
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.service_type not in (
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ):
        messages.error(request, "Computation flow is only for desludging requests.")
        return redirect("services:request_detail", pk=pk)
    if "[NO_INSPECTION_FEE]" not in (service_request.notes or ""):
        messages.warning(request, "Inspection must be waived before using this action.")
        return redirect("services:request_detail", pk=pk)
    if service_request.status not in _WAIVED_COMPUTATION_ELIGIBLE_STATUSES:
        messages.info(request, "This request is not in a state to create computation.")
        return redirect("services:request_detail", pk=pk)

    if not service_request.waived_inspection_crew_ready:
        messages.warning(
            request,
            "Assign the driver and helpers (as needed) before creating the computation.",
        )
        return redirect("dashboard:assign_waived_inspection_crew", pk=pk)

    outcome = _try_create_initial_computation_for_waived_request(service_request, request.user)
    if outcome == "created":
        messages.success(request, "Computation created. You can now edit and finalize it to send to the customer.")
    elif outcome == "exists":
        pass
    return redirect("services:edit_computation", pk=pk)


@login_required
@role_required("ADMIN")
def assign_waived_inspection_crew(request, pk):
    """
    When site inspection is waived, record driver/helpers so personnel count is set
    for computation (and can be updated after computation is sent).
    """
    from services.models import DesludgingPersonnel

    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.service_type not in (
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ):
        messages.error(request, "Crew assignment for waived inspection applies only to desludging requests.")
        return redirect("services:request_detail", pk=pk)
    if "[NO_INSPECTION_FEE]" not in (service_request.notes or ""):
        messages.error(request, "This request does not use the waived-inspection workflow.")
        return redirect("services:request_detail", pk=pk)
    if service_request.status not in _WAIVED_CREW_ASSIGNMENT_STATUSES:
        messages.error(request, "This request is not in a state for crew assignment.")
        return redirect("services:request_detail", pk=pk)

    drivers = DesludgingPersonnel.objects.filter(
        role=DesludgingPersonnel.Role.DRIVER, is_active=True
    ).order_by("full_name")
    helpers = DesludgingPersonnel.objects.filter(
        role=DesludgingPersonnel.Role.HELPER, is_active=True
    ).order_by("full_name")

    posted = {
        "driver_name": service_request.waived_crew_driver_name or "",
        "helper1": service_request.waived_crew_helper1_name or "",
        "helper2": service_request.waived_crew_helper2_name or "",
        "helper3": service_request.waived_crew_helper3_name or "",
    }

    if request.method == "POST":
        driver_name = (request.POST.get("waived_crew_driver_name") or "").strip()
        h1 = (request.POST.get("waived_crew_helper1_name") or "").strip()
        h2 = (request.POST.get("waived_crew_helper2_name") or "").strip()
        h3 = (request.POST.get("waived_crew_helper3_name") or "").strip()
        posted = {
            "driver_name": driver_name,
            "helper1": h1,
            "helper2": h2,
            "helper3": h3,
        }

        if not driver_name:
            messages.error(request, "Select or enter a driver.")
        else:
            service_request.assigned_inspector = None
            service_request.waived_crew_driver_name = driver_name
            service_request.waived_crew_helper1_name = h1
            service_request.waived_crew_helper2_name = h2
            service_request.waived_crew_helper3_name = h3
            service_request.save(
                update_fields=[
                    "assigned_inspector",
                    "waived_crew_driver_name",
                    "waived_crew_helper1_name",
                    "waived_crew_helper2_name",
                    "waived_crew_helper3_name",
                    "updated_at",
                ]
            )
            comp = getattr(service_request, "computation", None)
            if comp:
                comp.personnel_count = service_request.waived_inspection_personnel_count
                comp.save()
                service_request.fee_amount = comp.total_charge
                service_request.save(update_fields=["fee_amount"])
            messages.success(
                request,
                "Crew saved. Personnel count is updated on the computation when one exists.",
            )
            return redirect("services:request_detail", pk=pk)

    return render(
        request,
        "dashboard/assign_waived_inspection_crew.html",
        {
            "service_request": service_request,
            "personnel_drivers": drivers,
            "personnel_helpers": helpers,
            "posted": posted,
        },
    )


@login_required
@role_required("ADMIN")
def schedule_desludging(request, pk):
    """Admin sets the desludging date after payment is confirmed."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)

    # Derive any existing scheduled time from notes for comparison / initial display.
    existing_date_str = service_request.scheduled_desludging_date.isoformat() if service_request.scheduled_desludging_date else ""
    existing_time = ""
    if service_request.notes:
        dl_marker = "Desludging scheduled on "
        dl_idx = service_request.notes.rfind(dl_marker)
        if dl_idx != -1:
            dl_segment = service_request.notes[dl_idx + len(dl_marker):]
            try:
                if " at " in dl_segment:
                    _, time_part = dl_segment.split(" at ", 1)
                    if "Reason:" in time_part:
                        time_only, _ = time_part.split("Reason:", 1)
                        existing_time = time_only.strip().rstrip(".")
                    else:
                        existing_time = time_part.strip().rstrip(".")
            except ValueError:
                pass

    # Only when a schedule already exists is "Reason for Changes" required (reschedule flow).
    has_existing_schedule = bool((existing_date_str or "").strip()) or bool(
        (existing_time or "").strip()
    )

    if request.method == "POST":
        sched_date = request.POST.get("desludging_date")
        sched_time = (request.POST.get("desludging_time") or "").strip()
        change_reason = (request.POST.get("change_reason") or "").strip()
        if not sched_date or not sched_time:
            messages.error(request, "Please choose both a desludging date and time.")
            return render(request, "dashboard/schedule_desludging.html", {
                "service_request": service_request,
                "initial_date": sched_date or existing_date_str,
                "initial_time": sched_time or existing_time,
                "change_reason": change_reason,
                "has_existing_schedule": has_existing_schedule,
            })

        # Check if schedule actually changed (vs. prior saved schedule, not vs. empty initial).
        changed = (sched_date != existing_date_str) or (sched_time != (existing_time or ""))
        if has_existing_schedule and changed and not change_reason:
            messages.error(request, "Please provide a reason for the changes to the desludging schedule.")
            return render(request, "dashboard/schedule_desludging.html", {
                "service_request": service_request,
                "initial_date": sched_date,
                "initial_time": sched_time,
                "change_reason": change_reason,
                "has_existing_schedule": has_existing_schedule,
            })

        service_request.scheduled_desludging_date = sched_date
        service_request.status = ServiceRequest.Status.DESLUDGING_SCHEDULED

        # Append schedule info to notes for display/history.
        existing_notes = service_request.notes or ""
        line = f"Desludging scheduled on {sched_date} at {sched_time}."
        if has_existing_schedule and changed and change_reason:
            line += f" Reason: {change_reason}"
        service_request.notes = (existing_notes + "\n" if existing_notes else "") + line
        service_request.save(update_fields=["scheduled_desludging_date", "status", "notes"])

        reason_suffix = (
            f" Reason for latest change: {change_reason}"
            if has_existing_schedule and changed and change_reason
            else ""
        )
        schedule_note = (
            "Schedule may change due to operational or weather conditions; "
            "you will be notified if there are updates."
        )

        Notification.objects.create(
            user=service_request.consumer,
            message=(
                f"Your desludging has been scheduled for {sched_date} at {sched_time}. "
                + schedule_note
                + reason_suffix
            ),
            notification_type=Notification.NotificationType.DESLUDGING_SCHEDULED,
            related_request=service_request,
        )
        # Same update for the account that submitted the request on behalf of the consumer (if different).
        if (
            service_request.requested_by_id
            and service_request.requested_by_id != service_request.consumer_id
        ):
            Notification.objects.create(
                user=service_request.requested_by,
                message=(
                    f"Desludging for {service_request.client_name} has been scheduled for "
                    f"{sched_date} at {sched_time}. "
                    + schedule_note
                    + reason_suffix
                ),
                notification_type=Notification.NotificationType.DESLUDGING_SCHEDULED,
                related_request=service_request,
            )

        messages.success(
            request,
            "Desludging date and time scheduled; notifications were sent to the customer"
            + (
                " and the account that submitted the request."
                if service_request.requested_by_id
                and service_request.requested_by_id != service_request.consumer_id
                else "."
            ),
        )
        return redirect("services:request_detail", pk=pk)

    return render(request, "dashboard/schedule_desludging.html", {
        "service_request": service_request,
        "initial_date": existing_date_str,
        "initial_time": existing_time,
        "change_reason": "",
        "has_existing_schedule": has_existing_schedule,
    })


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
    tab = (request.GET.get("tab") or "account_management").strip()
    if tab not in ("account_management", "service_history", "previous_account_registration"):
        tab = "account_management"
    search = (request.GET.get("q") or "").strip()

    consumers = (
        _membership_consumer_qs()
        .select_related("consumer_profile")
        .order_by("last_name", "first_name", "id")
    )
    previous_form = None
    previous_records = User.objects.none()

    if tab == "previous_account_registration":
        from dashboard.forms import PreviousAccountRegistrationForm

        previous_form = PreviousAccountRegistrationForm(request.POST or None)
        previous_records = (
            _membership_consumer_qs()
            .filter(is_legacy_record=True)
            .select_related("consumer_profile")
            .order_by("-date_joined", "-id")
        )
        if search:
            previous_records = previous_records.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(username__icontains=search)
                | Q(consumer_profile__barangay__icontains=search)
                | Q(consumer_profile__street_address__icontains=search)
                | Q(consumer_profile__mobile_number__icontains=search)
            )

        if request.method == "POST" and previous_form.is_valid():
            cd = previous_form.cleaned_data
            fname = (cd["first_name"] or "").strip()
            lname = (cd["last_name"] or "").strip()
            brgy = (cd["barangay"] or "").strip()
            street = (cd["street_address"] or "").strip()
            prior_vol = cd.get("prior_desludging_m3_4y")
            if prior_vol is None:
                prior_vol = 0

            existing_real = (
                _membership_consumer_qs()
                .filter(
                    is_legacy_record=False,
                    first_name__iexact=fname,
                    last_name__iexact=lname,
                    consumer_profile__barangay__iexact=brgy,
                    consumer_profile__street_address__iexact=street,
                )
                .select_related("consumer_profile")
                .first()
            )

            if existing_real:
                with transaction.atomic():
                    prof = existing_real.consumer_profile
                    prof.prior_desludging_m3_4y = prior_vol
                    prof.last_cycle_request_date = cd.get("last_cycle_request_date")
                    if cd.get("mobile_number"):
                        prof.mobile_number = cd["mobile_number"]
                    prof.save()

                    waived_ids = []
                    if prior_vol > 0:
                        pending_fee_reqs = ServiceRequest.objects.filter(
                            consumer=existing_real,
                            status__in=[
                                ServiceRequest.Status.INSPECTION_FEE_DUE,
                                ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION,
                            ],
                        )
                        for sr in pending_fee_reqs:
                            sr.inspection_fee_paid = True
                            sr.status = ServiceRequest.Status.UNDER_REVIEW
                            sr.notes = (
                                ((sr.notes or "") + "\n") if sr.notes else ""
                            ) + "[NO_INSPECTION_FEE] Inspection fee waived — prior desludging record confirmed by admin."
                            sr.save(update_fields=["inspection_fee_paid", "status", "notes"])
                            waived_ids.append(str(sr.id))
                            Notification.objects.create(
                                user=existing_real,
                                message=(
                                    f"Request #{sr.id}: Inspection fee waived based on your prior desludging record. "
                                    "No site inspection is required. Open the request to assign crew (driver/helpers), "
                                    "then proceed to computation."
                                ),
                                notification_type=Notification.NotificationType.STATUS_CHANGE,
                                related_request=sr,
                            )

                msg = f"Account for {existing_real.get_full_name()} updated with prior desludging record."
                if waived_ids:
                    msg += f" Inspection fee waived on request(s) #{', #'.join(waived_ids)}. Assign crew on each request before computation."
                messages.success(request, msg)
                return redirect(f"{reverse('dashboard:admin_membership')}?tab=previous_account_registration")

            duplicate_legacy = (
                _membership_consumer_qs()
                .filter(
                    is_legacy_record=True,
                    first_name__iexact=fname,
                    last_name__iexact=lname,
                    consumer_profile__barangay__iexact=brgy,
                    consumer_profile__street_address__iexact=street,
                )
                .exists()
            )
            if duplicate_legacy:
                previous_form.add_error(
                    None,
                    "A previous registration with the same name and address already exists.",
                )
            else:
                base = slugify(f"{cd['first_name']}-{cd['last_name']}") or "legacy-consumer"
                username = f"legacy-{base}"
                suffix = 1
                while User.objects.filter(username=username).exists():
                    suffix += 1
                    username = f"legacy-{base}-{suffix}"

                with transaction.atomic():
                    legacy_user = User(
                        username=username,
                        first_name=fname,
                        last_name=lname,
                        email="",
                        role=User.Role.CONSUMER,
                        is_active=True,
                        is_approved=True,
                        is_legacy_record=True,
                    )
                    legacy_user.set_unusable_password()
                    legacy_user.save()
                    ConsumerProfile.objects.create(
                        user=legacy_user,
                        mobile_number=cd.get("mobile_number") or "",
                        street_address=street,
                        barangay=brgy,
                        municipality=(cd.get("municipality") or "").strip() or "Bayawan City",
                        province=(cd.get("province") or "").strip() or "Negros Oriental",
                        prior_desludging_m3_4y=prior_vol,
                        last_cycle_request_date=cd.get("last_cycle_request_date"),
                    )

                messages.success(
                    request,
                    "Previous customer registration saved. When a consumer registers with "
                    "matching information they will be notified and their data will be synced.",
                )
                return redirect(f"{reverse('dashboard:admin_membership')}?tab=previous_account_registration")
    else:
        if search:
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

    existing_accounts = User.objects.none()
    if tab == "previous_account_registration":
        existing_accounts = (
            _membership_consumer_qs()
            .filter(is_legacy_record=False, consumer_profile__isnull=False)
            .select_related("consumer_profile")
            .order_by("last_name", "first_name")
        )

    context = {
        "consumers": consumers,
        "active_tab": tab,
        "search_query": search,
        "previous_form": previous_form,
        "previous_records": previous_records,
        "existing_accounts": existing_accounts,
    }
    return render(request, "dashboard/admin_membership.html", context)


@login_required
@role_required("ADMIN")
def admin_equipment(request):
    """Maintain declogger / service equipment list for completion-form dropdowns."""
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "add":
            unit_number = (request.POST.get("unit_number") or "").strip()
            notes = (request.POST.get("notes") or "").strip()
            if not unit_number:
                messages.error(request, "Enter a declogger or unit number.")
            elif ServiceEquipment.objects.filter(unit_number__iexact=unit_number).exists():
                messages.error(request, "That unit number already exists.")
            else:
                ServiceEquipment.objects.create(unit_number=unit_number, notes=notes)
                messages.success(request, f"Added equipment {unit_number}.")
            return redirect("dashboard:admin_equipment")

        if action == "delete":
            raw_id = request.POST.get("equipment_id")
            try:
                eq = ServiceEquipment.objects.get(pk=int(raw_id))
            except (ValueError, TypeError, ServiceEquipment.DoesNotExist):
                messages.error(request, "Equipment entry not found.")
            else:
                label = eq.unit_number
                eq.delete()
                messages.success(request, f"Removed equipment {label}.")
            return redirect("dashboard:admin_equipment")

        if action == "toggle_active":
            raw_id = request.POST.get("equipment_id")
            try:
                eq = ServiceEquipment.objects.get(pk=int(raw_id))
            except (ValueError, TypeError, ServiceEquipment.DoesNotExist):
                messages.error(request, "Equipment entry not found.")
            else:
                eq.is_active = not eq.is_active
                eq.save(update_fields=["is_active"])
                state = "active" if eq.is_active else "inactive"
                messages.success(request, f"{eq.unit_number} is now {state}.")
            return redirect("dashboard:admin_equipment")

        messages.error(
            request,
            "That equipment action was not recognized (the form may be outdated). Refresh the Equipment page and try again.",
        )
        return redirect("dashboard:admin_equipment")

    equipment_list = ServiceEquipment.objects.all().order_by("unit_number")
    return render(
        request,
        "dashboard/admin_equipment.html",
        {"equipment_list": equipment_list},
    )


@login_required
@role_required("ADMIN")
@require_POST
def admin_update_prior_volume(request, user_id):
    """Quick-update a consumer's prior desludging volume and waive pending inspection fees."""
    consumer = get_object_or_404(
        User,
        pk=user_id,
        role=User.Role.CONSUMER,
        is_legacy_record=False,
        is_superuser=False,
        is_staff=False,
    )
    raw = (request.POST.get("prior_desludging_m3_4y") or "0").strip().replace(",", "")
    try:
        volume = max(0, int(round(float(raw))))
    except (ValueError, TypeError, OverflowError):
        volume = 0

    raw_cycle_date = (request.POST.get("last_cycle_request_date") or "").strip()
    last_cycle_date = None
    if raw_cycle_date:
        last_cycle_date = parse_date(raw_cycle_date)
        if last_cycle_date is None:
            messages.error(request, "Invalid date for last request for cycle. Use YYYY-MM-DD or leave blank.")
            return redirect(f"{reverse('dashboard:admin_membership')}?tab=previous_account_registration")

    if volume > 0 and last_cycle_date is None:
        messages.error(
            request,
            "Last service date is required: enter the date of last request for cycle before saving a prior volume above zero.",
        )
        return redirect(f"{reverse('dashboard:admin_membership')}?tab=previous_account_registration")

    waived_ids = []
    with transaction.atomic():
        prof, _ = ConsumerProfile.objects.get_or_create(user=consumer)
        prof.prior_desludging_m3_4y = volume
        prof.last_cycle_request_date = last_cycle_date
        prof.save(update_fields=["prior_desludging_m3_4y", "last_cycle_request_date"])

        if volume > 0:
            pending_fee_reqs = ServiceRequest.objects.filter(
                consumer=consumer,
                status__in=[
                    ServiceRequest.Status.INSPECTION_FEE_DUE,
                    ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION,
                ],
            )
            for sr in pending_fee_reqs:
                sr.inspection_fee_paid = True
                sr.status = ServiceRequest.Status.UNDER_REVIEW
                sr.notes = (
                    ((sr.notes or "") + "\n") if sr.notes else ""
                ) + "[NO_INSPECTION_FEE] Inspection fee waived — prior desludging record confirmed by admin."
                sr.save(update_fields=["inspection_fee_paid", "status", "notes"])
                waived_ids.append(str(sr.id))
                Notification.objects.create(
                    user=consumer,
                    message=(
                        f"Request #{sr.id}: Inspection fee waived based on your prior desludging record. "
                        "No site inspection is required. Open the request to assign crew (driver/helpers), "
                        "then proceed to computation."
                    ),
                    notification_type=Notification.NotificationType.STATUS_CHANGE,
                    related_request=sr,
                )

    display = consumer.get_full_name() or consumer.username
    msg = f"Prior desludging volume for {display} updated to {volume} m³."
    if last_cycle_date:
        msg += f" Last cycle request date set to {last_cycle_date.strftime('%b %d, %Y')}."
    else:
        msg += " Last cycle request date cleared."
    if waived_ids:
        msg += (
            f" Inspection fee waived on request(s) #{', #'.join(waived_ids)}. "
            "Assign crew on each request before computation."
        )
    messages.success(request, msg)
    return redirect(f"{reverse('dashboard:admin_membership')}?tab=previous_account_registration")


@login_required
@role_required("ADMIN")
@require_POST
def admin_reset_consumer_password(request, user_id):
    """
    Set a consumer's password to the system default temporary password and
    require them to choose a new password on next login.
    """
    consumer = get_object_or_404(
        User,
        pk=user_id,
        role=User.Role.CONSUMER,
        is_superuser=False,
        is_staff=False,
    )
    consumer.set_password(CONSUMER_DEFAULT_RESET_PASSWORD)
    consumer.must_change_password = True
    consumer.save(update_fields=["password", "must_change_password"])
    display_name = consumer.get_full_name() or consumer.username
    messages.success(
        request,
        f'Password reset for {display_name}. Temporary password: {CONSUMER_DEFAULT_RESET_PASSWORD} '
        "(share this with the member securely). They must set a new password after signing in.",
    )
    params = {"tab": "account_management"}
    q = (request.POST.get("next_q") or "").strip()
    if q:
        params["q"] = q
    url = reverse("dashboard:admin_membership")
    if params:
        url = f"{url}?{urlencode(params)}"
    return redirect(url)


@login_required
@role_required("ADMIN")
def member_service_history(request, user_id):
    """Service history for a specific member"""
    consumer = get_object_or_404(
        User,
        pk=user_id,
        role=User.Role.CONSUMER,
        is_superuser=False,
        is_staff=False,
    )
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
            from dashboard.models import compute_quick_desludging_estimate

            category = form.cleaned_data["category"]
            location = form.cleaned_data["location"]
            cubic_meters = form.cleaned_data["cubic_meters"]
            distance = form.cleaned_data.get("distance_km", Decimal("0")) or Decimal("0")
            personnel_count = form.cleaned_data.get("personnel_count") or 4
            connected = form.cleaned_data.get("connected_to_bawad") == "YES"
            public_private = form.cleaned_data.get("public_private") or "PRIVATE"
            bawad_prior = form.cleaned_data.get("bawad_prior_used_m3") or Decimal("0")

            est = compute_quick_desludging_estimate(
                category=category,
                location=location,
                cubic_meters=cubic_meters,
                distance_km=distance,
                personnel_count=int(personnel_count),
                meals_transport_override=None,
                connected_to_bawad=connected,
                public_private=public_private,
                bawad_prior_used_m3=bawad_prior,
            )

            computation_result = {
                **est,
                "prepared_by": request.user.get_full_name() or request.user.username,
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
