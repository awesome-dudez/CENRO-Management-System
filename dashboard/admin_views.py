from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.urls import reverse
from django.utils.text import slugify
from django.utils import timezone
from django.views.decorators.http import require_POST
from urllib.parse import urlencode

from accounts.constants import CONSUMER_DEFAULT_RESET_PASSWORD
from accounts.decorators import role_required
from accounts.models import ConsumerProfile, User
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
@role_required("ADMIN", "STAFF")
def admin_requests(request):
    """Admin requests view with sub-tabs"""
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
            status=ServiceRequest.Status.COMPLETED,
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
            messages.error(request, "Selected inspector is invalid.")
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
            service_request.save(update_fields=["notes"])

        # If a computation already exists, recalculate without inspection fee.
        computation = getattr(service_request, "computation", None)
        if computation:
            computation.save()

        messages.success(
            request,
            "Inspection has been waived for this request. Any inspection fee will be removed from the computation.",
        )
    return redirect("services:request_detail", pk=pk)


@login_required
@role_required("ADMIN")
def proceed_to_computation(request, pk):
    """
    When inspection has been waived, create an initial computation (if none exists)
    and send the admin to edit/finalize it. Allows the request to move to the next step.
    """
    from dashboard.models import ServiceComputation
    from services.location import distance_from_cenro

    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.service_type not in (
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ):
        messages.error(request, "Computation flow is only for desludging requests.")
        return redirect("services:request_detail", pk=pk)
    if service_request.status not in (
        ServiceRequest.Status.UNDER_REVIEW,
        ServiceRequest.Status.SUBMITTED,
    ):
        messages.info(request, "This request is not in a state to create computation.")
        return redirect("services:request_detail", pk=pk)
    if "[NO_INSPECTION_FEE]" not in (service_request.notes or ""):
        messages.warning(request, "Inspection must be waived before using this action.")
        return redirect("services:request_detail", pk=pk)

    computation = getattr(service_request, "computation", None)
    if not computation:
        is_outside = not service_request.is_within_bayawan
        dist = Decimal("0")
        if service_request.gps_latitude and service_request.gps_longitude:
            km = distance_from_cenro(
                float(service_request.gps_latitude),
                float(service_request.gps_longitude),
            )
            dist = Decimal(str(round(km, 2)))
        computation = ServiceComputation.objects.create(
            service_request=service_request,
            is_outside_bayawan=is_outside,
            cubic_meters=service_request.cubic_meters or Decimal("5"),
            distance_km=dist,
            trips=1,
            personnel_count=4,
            prepared_by=request.user,
        )
        computation.is_finalized = False
        computation.save()
        service_request.fee_amount = computation.total_charge
        service_request.save(update_fields=["fee_amount"])
        messages.success(request, "Computation created. You can now edit and finalize it to send to the customer.")
    return redirect("services:edit_computation", pk=pk)


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

    consumers = User.objects.filter(role=User.Role.CONSUMER).select_related("consumer_profile")
    previous_form = None
    previous_records = User.objects.none()

    if tab == "previous_account_registration":
        from dashboard.forms import PreviousAccountRegistrationForm

        previous_form = PreviousAccountRegistrationForm(request.POST or None)
        previous_records = (
            User.objects.filter(role=User.Role.CONSUMER, is_legacy_record=True)
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
            duplicate = User.objects.filter(
                role=User.Role.CONSUMER,
                is_legacy_record=True,
                first_name__iexact=(cd["first_name"] or "").strip(),
                last_name__iexact=(cd["last_name"] or "").strip(),
                consumer_profile__barangay__iexact=(cd["barangay"] or "").strip(),
                consumer_profile__street_address__iexact=(cd["street_address"] or "").strip(),
            ).exists()
            if duplicate:
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
                        first_name=(cd["first_name"] or "").strip(),
                        last_name=(cd["last_name"] or "").strip(),
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
                        street_address=(cd.get("street_address") or "").strip(),
                        barangay=(cd.get("barangay") or "").strip(),
                        municipality=(cd.get("municipality") or "").strip() or "Bayawan City",
                        province=(cd.get("province") or "").strip() or "Negros Oriental",
                        prior_desludging_m3_4y=cd.get("prior_desludging_m3_4y") or Decimal("0"),
                    )

                messages.success(
                    request,
                    "Previous customer registration saved. This record can now be matched during request verification.",
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

    context = {
        "consumers": consumers,
        "active_tab": tab,
        "search_query": search,
        "previous_form": previous_form,
        "previous_records": previous_records,
    }
    return render(request, "dashboard/admin_membership.html", context)


@login_required
@role_required("ADMIN")
@require_POST
def admin_reset_consumer_password(request, user_id):
    """
    Set a consumer's password to the system default temporary password and
    require them to choose a new password on next login.
    """
    consumer = get_object_or_404(User, pk=user_id, role=User.Role.CONSUMER)
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
