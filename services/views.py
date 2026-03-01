from datetime import date as date_cls, datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.views.decorators.csrf import ensure_csrf_cookie
import io
import json
import time
import os
from uuid import uuid4

from accounts.decorators import role_required
from accounts.models import User
from scheduling.models import Schedule

from .forms import (
    ServiceRequestForm,
    ServiceRequestStep1Form,
    ServiceRequestStep2Form,
    ServiceRequestStep3Form,
)
from .business_days import next_business_day, ph_holidays
from .location import detect_barangay_for_point
from .geocode import address_in_bayawan, extract_barangay, reverse_geocode_osm
from .models import CompletionInfo, InspectionDetail, Notification, ServiceRequest


# ---------------------------------------------------------------------------
# Multi-step service request wizard (3 steps)
# ---------------------------------------------------------------------------

@login_required
@role_required("CONSUMER")
def create_request(request):
    """3-step service request wizard."""
    step = request.GET.get("step", 1)
    try:
        step = int(step)
    except ValueError:
        step = 1

    if "service_request_data" not in request.session:
        request.session["service_request_data"] = {}

    form_data = request.session.get("service_request_data", {})

    if request.method == "POST":
        # ---- Step 1: Service Type ----
        if step == 1:
            form = ServiceRequestStep1Form(request.POST)
            if form.is_valid():
                form_data["service_type"] = form.cleaned_data["service_type"]
                request.session["service_request_data"] = form_data
                return HttpResponseRedirect(reverse("services:create_request") + "?step=2")

        # ---- Step 2: Customer Request Form ----
        elif step == 2:
            form = ServiceRequestStep2Form(request.POST, request.FILES)
            if form.is_valid():
                lat = form.cleaned_data.get("gps_latitude")
                lon = form.cleaned_data.get("gps_longitude")
                loc_mode = form.cleaned_data.get("location_mode") or "PIN"
                form_data.update({
                    "client_name": form.cleaned_data["client_name"],
                    "request_date": str(form.cleaned_data["request_date"]),
                    "contact_number": form.cleaned_data["contact_number"],
                    "location_mode": loc_mode,
                    "barangay": form.cleaned_data.get("barangay") or "",
                    "address": form.cleaned_data.get("address") or "",
                    "gps_latitude": float(lat) if lat is not None else None,
                    "gps_longitude": float(lon) if lon is not None else None,
                    "connected_to_bawad": form.cleaned_data["connected_to_bawad"],
                    "public_private": form.cleaned_data["public_private"],
                })
                if form.cleaned_data.get("bawad_proof"):
                    request.session["_bawad_proof_pending"] = True
                if form.cleaned_data.get("client_signature"):
                    request.session["_client_sig_pending"] = True
                if loc_mode == "TEXT":
                    photo_paths = []
                    for key in ("location_photo_1", "location_photo_2"):
                        f = request.FILES.get(key)
                        if f:
                            ext = os.path.splitext(f.name)[1] or ".jpg"
                            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                                ext = ".jpg"
                            path = f"location_photos/temp/{uuid4()}{ext}"
                            default_storage.save(path, ContentFile(f.read()))
                            photo_paths.append(path)
                    form_data["_location_photos"] = photo_paths
                else:
                    form_data.pop("_location_photos", None)
                request.session["service_request_data"] = form_data
                return HttpResponseRedirect(reverse("services:create_request") + "?step=3")

        # ---- Step 3: Review & Submit ----
        elif step == 3:
            form = ServiceRequestStep3Form(request.POST)
            if form.is_valid() and form_data:
                gps_latitude = None
                gps_longitude = None
                if form_data.get("gps_latitude"):
                    try:
                        gps_latitude = Decimal(str(form_data["gps_latitude"]))
                    except (ValueError, TypeError):
                        pass
                if form_data.get("gps_longitude"):
                    try:
                        gps_longitude = Decimal(str(form_data["gps_longitude"]))
                    except (ValueError, TypeError):
                        pass

                service_request = ServiceRequest.objects.create(
                    consumer=request.user,
                    client_name=form_data.get("client_name", request.user.get_full_name()),
                    request_date=datetime.strptime(form_data["request_date"], "%Y-%m-%d").date()
                    if form_data.get("request_date")
                    else timezone.now().date(),
                    contact_number=form_data.get("contact_number", ""),
                    location_mode=form_data.get("location_mode", "PIN"),
                    barangay=form_data.get("barangay", ""),
                    address=form_data.get("address", ""),
                    gps_latitude=gps_latitude,
                    gps_longitude=gps_longitude,
                    service_type=form_data.get("service_type", "RESIDENTIAL_DESLUDGING"),
                    connected_to_bawad=form_data.get("connected_to_bawad") == "YES",
                    public_private=form_data.get("public_private", "PRIVATE"),
                    status=ServiceRequest.Status.SUBMITTED,
                )
                photo_paths = form_data.get("_location_photos") or []
                for i, path in enumerate(photo_paths[:2]):
                    if default_storage.exists(path):
                        with default_storage.open(path, "rb") as fp:
                            name = os.path.basename(path)
                            if i == 0:
                                service_request.location_photo_1.save(name, ContentFile(fp.read()), save=True)
                            else:
                                service_request.location_photo_2.save(name, ContentFile(fp.read()), save=True)
                        try:
                            default_storage.delete(path)
                        except Exception:
                            pass

                # Notify all admins
                admin_users = User.objects.filter(role=User.Role.ADMIN)
                for admin in admin_users:
                    Notification.objects.create(
                        user=admin,
                        message=f"New {service_request.get_service_type_display()} request from {service_request.client_name}.",
                        notification_type=Notification.NotificationType.REQUEST_SUBMITTED,
                        related_request=service_request,
                    )

                # Notify consumer
                Notification.objects.create(
                    user=request.user,
                    message=f"Your {service_request.get_service_type_display()} request has been submitted.",
                    notification_type=Notification.NotificationType.STATUS_CHANGE,
                    related_request=service_request,
                )

                reference_number = f"ECO-{timezone.now().year}-{service_request.id % 1000:03d}"

                # Clean up session
                request.session.pop("service_request_data", None)
                request.session.pop("_bawad_proof_pending", None)
                request.session.pop("_client_sig_pending", None)
                request.session.pop("_location_photos", None)

                messages.success(request, "Service request submitted successfully!")
                return render(request, "services/request_success.html", {
                    "reference_number": reference_number,
                    "service_request": service_request,
                })

    # GET -- prepare form
    if step == 1:
        form = ServiceRequestStep1Form(initial={"service_type": form_data.get("service_type", "")})
    elif step == 2:
        default_contact = ""
        try:
            default_contact = request.user.consumer_profile.mobile_number or ""
        except Exception:
            pass
        form = ServiceRequestStep2Form(initial={
            "client_name": form_data.get("client_name", request.user.get_full_name()),
            "request_date": form_data.get("request_date", str(next_business_day())),
            "contact_number": form_data.get("contact_number", default_contact),
            "location_mode": form_data.get("location_mode", "PIN"),
            "connected_to_bawad": form_data.get("connected_to_bawad", "NO"),
            "public_private": form_data.get("public_private", "PRIVATE"),
            "barangay": form_data.get("barangay", ""),
            "address": form_data.get("address", ""),
            "gps_latitude": form_data.get("gps_latitude"),
            "gps_longitude": form_data.get("gps_longitude"),
        })
    elif step == 3:
        form = ServiceRequestStep3Form()
    else:
        form = ServiceRequestStep1Form()
        step = 1

    owner_profile = {}
    if step == 2:
        owner_profile["client_name"] = request.user.get_full_name()
        try:
            cp = request.user.consumer_profile
            owner_profile["contact_number"] = cp.mobile_number or ""
            owner_profile["address"] = cp.full_address or ""
            owner_profile["gps_latitude"] = float(cp.gps_latitude) if cp.gps_latitude else None
            owner_profile["gps_longitude"] = float(cp.gps_longitude) if cp.gps_longitude else None
        except Exception:
            owner_profile["contact_number"] = ""
            owner_profile["address"] = ""
            owner_profile["gps_latitude"] = None
            owner_profile["gps_longitude"] = None

    holidays_json = "[]"
    if step == 2:
        today = date_cls.today()
        all_holidays = ph_holidays(today.year) | ph_holidays(today.year + 1)
        holidays_json = json.dumps(sorted(d.isoformat() for d in all_holidays))

    context = {
        "form": form,
        "step": step,
        "form_data": form_data,
        "owner_profile_json": json.dumps(owner_profile),
        "holidays_json": holidays_json,
    }
    return render(request, "services/create_request_wizard.html", context)


# ---------------------------------------------------------------------------
# Reverse geocode proxy
# ---------------------------------------------------------------------------

_RG_CACHE: dict[str, tuple[float, dict]] = {}
_RG_CACHE_TTL_SECONDS = 60 * 10


def _rg_cache_get(key: str):
    now = time.time()
    item = _RG_CACHE.get(key)
    if not item:
        return None
    ts, val = item
    if now - ts > _RG_CACHE_TTL_SECONDS:
        _RG_CACHE.pop(key, None)
        return None
    return val


def _rg_cache_set(key: str, val: dict):
    _RG_CACHE[key] = (time.time(), val)


@login_required
def reverse_geocode(request):
    lat = request.GET.get("lat")
    lon = request.GET.get("lon")
    if not lat or not lon:
        return JsonResponse({"ok": False, "error": "Missing lat/lon"}, status=400)

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid lat/lon"}, status=400)

    detected = detect_barangay_for_point(lat_f, lon_f)

    cache_key = f"{round(lat_f, 6)},{round(lon_f, 6)}"
    cached = _rg_cache_get(cache_key)
    if cached:
        cached["barangay"] = detected or cached.get("barangay")
        cached["within_bayawan"] = bool(detected) or bool(cached.get("within_bayawan"))
        return JsonResponse(cached)

    data = reverse_geocode_osm(lat_f, lon_f)
    if not data:
        return JsonResponse({"ok": False, "error": "Reverse geocoding failed"}, status=502)

    address = data.get("address") or {}
    display_name = data.get("display_name")

    within_bayawan = bool(detected) or address_in_bayawan(address, display_name)
    barangay = detected or extract_barangay(address)

    payload = {
        "ok": True,
        "within_bayawan": within_bayawan,
        "barangay": barangay,
        "display_name": display_name,
        "address": address,
    }
    _rg_cache_set(cache_key, payload)
    return JsonResponse(payload)


# ---------------------------------------------------------------------------
# Service request views
# ---------------------------------------------------------------------------

@login_required
def request_list(request):
    if request.user.is_admin():
        requests_qs = ServiceRequest.objects.all().select_related("consumer").order_by("-created_at")
    elif request.user.is_staff_member():
        requests_qs = ServiceRequest.objects.filter(
            assigned_inspector=request.user
        ).select_related("consumer").order_by("-created_at")
    else:
        requests_qs = ServiceRequest.objects.filter(consumer=request.user).order_by("-created_at")
    return render(request, "services/request_list.html", {"requests": requests_qs})


@login_required
def request_detail(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if (
        not request.user.is_admin()
        and not request.user.is_staff_member()
        and service_request.consumer != request.user
    ):
        messages.error(request, "You do not have permission to view this request.")
        return redirect("services:request_list")

    staff_members = User.objects.filter(role__in=[User.Role.ADMIN, User.Role.STAFF])

    context = {
        "sr": service_request,
        "staff_members": staff_members,
    }
    return render(request, "services/request_detail.html", context)


@login_required
def history(request):
    if request.user.is_admin():
        requests_qs = ServiceRequest.objects.all().select_related("consumer").order_by("-created_at")
    else:
        requests_qs = ServiceRequest.objects.filter(consumer=request.user).order_by("-created_at")
    return render(request, "services/history.html", {"requests": requests_qs})


@login_required
@role_required("ADMIN")
def client_records(request):
    search = request.GET.get("search", "")
    barangay_filter = request.GET.get("barangay", "")
    consumers = User.objects.filter(role=User.Role.CONSUMER).select_related("consumer_profile").prefetch_related("service_requests")
    if search:
        consumers = consumers.filter(
            Q(first_name__icontains=search) | Q(last_name__icontains=search) | Q(username__icontains=search)
        )
    if barangay_filter:
        consumers = consumers.filter(consumer_profile__barangay=barangay_filter)
    barangays = User.objects.filter(role=User.Role.CONSUMER).values_list(
        "consumer_profile__barangay", flat=True
    ).distinct()
    consumer_data = []
    for consumer in consumers:
        last_declogging = (
            ServiceRequest.objects.filter(
                consumer=consumer,
                service_type__in=[
                    ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
                    ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
                ],
                status=ServiceRequest.Status.COMPLETED,
            )
            .order_by("-request_date")
            .first()
        )
        next_declogging = None
        if last_declogging:
            from datetime import timedelta
            next_declogging = last_declogging.request_date + timedelta(days=4 * 365)
        consumer_data.append({
            "consumer": consumer,
            "last_declogging": last_declogging.request_date if last_declogging else None,
            "next_declogging": next_declogging,
        })
    context = {"consumer_data": consumer_data, "barangays": barangays}
    return render(request, "services/client_records.html", context)


# ---------------------------------------------------------------------------
# Inspection + Completion views
# ---------------------------------------------------------------------------

@login_required
@role_required("ADMIN", "STAFF")
def submit_inspection(request, pk):
    """Admin/Staff fills inspection details."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if request.method == "POST":
        InspectionDetail.objects.update_or_create(
            service_request=service_request,
            defaults={
                "inspection_date": request.POST.get("inspection_date"),
                "inspected_by": request.POST.get("inspected_by", ""),
                "remarks": request.POST.get("remarks", ""),
            },
        )
        if request.FILES.get("inspector_signature"):
            insp = service_request.inspection_detail
            insp.inspector_signature = request.FILES["inspector_signature"]
            insp.save()

        service_request.status = ServiceRequest.Status.INSPECTED
        service_request.save()

        Notification.objects.create(
            user=service_request.consumer,
            message="Your service request has been inspected.",
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )
        messages.success(request, "Inspection details saved.")
        return redirect("services:request_detail", pk=pk)

    return render(request, "services/inspection_form.html", {"sr": service_request})


@login_required
@role_required("ADMIN", "STAFF")
def submit_completion(request, pk):
    """Admin/Staff fills completion info, triggers computation generation."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if request.method == "POST":
        completion, _ = CompletionInfo.objects.update_or_create(
            service_request=service_request,
            defaults={
                "date_completed": request.POST.get("date_completed"),
                "time_required": request.POST.get("time_required", ""),
                "witnessed_by_name": request.POST.get("witnessed_by_name", ""),
                "declogger_no": request.POST.get("declogger_no", ""),
                "fuel_consumption": request.POST.get("fuel_consumption") or None,
                "driver_name": request.POST.get("driver_name", ""),
                "helper1_name": request.POST.get("helper1_name", ""),
                "helper2_name": request.POST.get("helper2_name", ""),
                "helper3_name": request.POST.get("helper3_name", ""),
            },
        )
        for field_name in [
            "witnessed_by_signature", "driver_signature",
            "helper1_signature", "helper2_signature", "helper3_signature",
        ]:
            f = request.FILES.get(field_name)
            if f:
                setattr(completion, field_name, f)
        completion.save()

        # Auto-generate computation
        _auto_generate_computation(service_request, request.user)

        messages.success(request, "Completion info saved. Computation letter generated.")
        return redirect("services:request_detail", pk=pk)

    return render(request, "services/completion_form.html", {"sr": service_request})


def _auto_generate_computation(service_request, admin_user):
    """Create or update ServiceComputation after completion info is saved."""
    from dashboard.models import ServiceComputation
    from .location import distance_from_cenro

    completion = getattr(service_request, "completion_info", None)
    is_outside = not service_request.is_within_bayawan

    personnel = completion.personnel_count if completion else 4

    dist = Decimal("0")
    if service_request.gps_latitude and service_request.gps_longitude:
        km = distance_from_cenro(
            float(service_request.gps_latitude),
            float(service_request.gps_longitude),
        )
        dist = Decimal(str(round(km, 2)))

    comp, _ = ServiceComputation.objects.update_or_create(
        service_request=service_request,
        defaults={
            "is_outside_bayawan": is_outside,
            "cubic_meters": service_request.cubic_meters or Decimal("5"),
            "distance_km": dist,
            "trips": 1,
            "personnel_count": personnel,
            "prepared_by": admin_user,
        },
    )
    comp.save()

    service_request.fee_amount = comp.total_charge
    service_request.status = ServiceRequest.Status.COMPUTATION_SENT
    service_request.save()

    Notification.objects.create(
        user=service_request.consumer,
        message="Your computation letter is ready. You can now view and download it.",
        notification_type=Notification.NotificationType.COMPUTATION_READY,
        related_request=service_request,
    )


# ---------------------------------------------------------------------------
# Computation letter view
# ---------------------------------------------------------------------------

@login_required
def view_computation(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if (
        not request.user.is_admin()
        and not request.user.is_staff_member()
        and service_request.consumer != request.user
    ):
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")

    computation = getattr(service_request, "computation", None)
    if not computation:
        messages.warning(request, "Computation not yet available.")
        return redirect("services:request_detail", pk=pk)

    return render(request, "services/computation_letter.html", {
        "sr": service_request,
        "comp": computation,
    })


@login_required
def download_computation_pdf(request, pk):
    """Generate and return the computation letter as a PDF download."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if (
        not request.user.is_admin()
        and not request.user.is_staff_member()
        and service_request.consumer != request.user
    ):
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")

    computation = getattr(service_request, "computation", None)
    if not computation:
        messages.warning(request, "Computation not yet available.")
        return redirect("services:request_detail", pk=pk)

    try:
        from xhtml2pdf import pisa
    except ImportError:
        messages.error(
            request,
            "PDF download is not available. Install xhtml2pdf: pip install xhtml2pdf",
        )
        return redirect("services:view_computation", pk=pk)

    template = get_template("services/computation_letter_pdf.html")
    html = template.render({"sr": service_request, "comp": computation})
    result = io.BytesIO()
    pdf = pisa.pisaDocument(
        io.BytesIO(html.encode("utf-8")),
        result,
        encoding="utf-8",
    )
    if pdf.err:
        messages.error(request, "PDF generation failed.")
        return redirect("services:view_computation", pk=pk)

    filename = f"computation-ECO-{service_request.created_at.year}-{service_request.id:03d}.pdf"
    response = HttpResponse(result.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@role_required("ADMIN", "STAFF")
def edit_computation(request, pk):
    """Edit computation (admin/staff only). Recalculates charges on save."""
    from dashboard.forms import ServiceComputationForm
    from dashboard.models import ServiceComputation

    service_request = get_object_or_404(ServiceRequest, pk=pk)
    computation = getattr(service_request, "computation", None)
    if not computation:
        messages.warning(request, "Computation not yet available.")
        return redirect("services:request_detail", pk=pk)

    form = ServiceComputationForm(instance=computation)
    form.fields.pop("charge_category", None)
    form.fields.pop("trips", None)

    if request.method == "POST":
        form = ServiceComputationForm(request.POST, instance=computation)
        form.fields.pop("charge_category", None)
        form.fields.pop("trips", None)
        if form.is_valid():
            form.save()
            service_request.fee_amount = computation.total_charge
            service_request.save(update_fields=["fee_amount"])
            messages.success(request, "Computation updated. Charges recalculated.")
            return redirect("services:view_computation", pk=pk)

    return render(request, "services/computation_edit.html", {
        "form": form,
        "sr": service_request,
        "comp": computation,
    })


# ---------------------------------------------------------------------------
# Customer receipt upload
# ---------------------------------------------------------------------------

@login_required
def upload_receipt(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.consumer != request.user and not request.user.is_admin():
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")

    if request.method == "POST":
        receipt = request.FILES.get("treasurer_receipt")
        if receipt:
            # Only attach the receipt and move to Awaiting Payment.
            # Actual payment approval will be done by an admin.
            service_request.treasurer_receipt = receipt
            service_request.status = ServiceRequest.Status.AWAITING_PAYMENT
            service_request.save(update_fields=["treasurer_receipt", "status"])

            admin_users = User.objects.filter(role=User.Role.ADMIN)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Payment receipt uploaded by {service_request.client_name} for request #{service_request.id}.",
                    notification_type=Notification.NotificationType.PAYMENT_UPLOADED,
                    related_request=service_request,
                )
            messages.success(request, "Receipt uploaded successfully. Waiting for admin verification.")
        else:
            messages.error(request, "Please select a file to upload.")
        return redirect("services:request_detail", pk=pk)

    return render(request, "services/upload_receipt.html", {"sr": service_request})


# ---------------------------------------------------------------------------
# Print application
# ---------------------------------------------------------------------------

@login_required
def print_application(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    inspection = getattr(service_request, "inspection_detail", None)
    completion = getattr(service_request, "completion_info", None)
    return render(request, "services/print_application.html", {
        "sr": service_request,
        "inspection": inspection,
        "completion": completion,
    })


# ---------------------------------------------------------------------------
# Mark request complete
# ---------------------------------------------------------------------------

@login_required
@role_required("ADMIN", "STAFF")
def complete_request(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    service_request.status = ServiceRequest.Status.COMPLETED
    service_request.save()
    Notification.objects.create(
        user=service_request.consumer,
        message=f"Your {service_request.get_service_type_display()} request has been completed.",
        notification_type=Notification.NotificationType.STATUS_CHANGE,
        related_request=service_request,
    )
    messages.success(request, "Request marked as completed.")
    return redirect("services:request_detail", pk=pk)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@login_required
def notification_list(request):
    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")[:50]
    return render(request, "services/notifications.html", {"notifications": notifications})


@login_required
def mark_notification_read(request, pk):
    notif = get_object_or_404(Notification, pk=pk, user=request.user)
    notif.is_read = True
    notif.save()
    if notif.related_request:
        sr = notif.related_request
        ntype = notif.notification_type
        if request.user.is_admin() or request.user.is_staff_member():
            if ntype == Notification.NotificationType.REQUEST_SUBMITTED:
                return redirect("dashboard:admin_requests")
            elif ntype == Notification.NotificationType.PAYMENT_UPLOADED:
                return redirect("services:request_detail", pk=sr.pk)
            return redirect("services:request_detail", pk=sr.pk)
        else:
            if ntype == Notification.NotificationType.COMPUTATION_READY:
                return redirect("services:view_computation", pk=sr.pk)
            elif ntype == Notification.NotificationType.DESLUDGING_SCHEDULED:
                return redirect("services:request_detail", pk=sr.pk)
            return redirect("services:request_detail", pk=sr.pk)
    return redirect("services:notification_list")


@login_required
def notifications_count_api(request):
    """JSON API: return only unread count (for badge)."""
    count = Notification.objects.filter(user=request.user, is_read=False).count()
    return JsonResponse({"unread_count": count})


@login_required
@ensure_csrf_cookie
def notifications_api(request):
    """JSON API: return unread count and recent notifications (for dropdown details)."""
    qs = Notification.objects.filter(user=request.user).order_by("-created_at")[:15]
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    items = []
    for n in qs:
        items.append({
            "id": n.pk,
            "message": n.message,
            "type": n.notification_type,
            "is_read": n.is_read,
            "created_at": n.created_at.strftime("%b %d, %Y %I:%M %p"),
            "mark_url": reverse("services:mark_notification_read", args=[n.pk]),
        })
    return JsonResponse({"unread_count": unread_count, "notifications": items})


@login_required
def mark_all_notifications_read(request):
    """Mark all of the current user's notifications as read."""
    if request.method == "POST":
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return JsonResponse({"ok": True})
    return JsonResponse({"ok": False}, status=405)
