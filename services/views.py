from datetime import datetime, timedelta
from decimal import Decimal
import base64

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
import io
import json
import time
import os
from uuid import uuid4

from accounts.decorators import role_required
from accounts.models import User
from scheduling.models import Schedule
from django.templatetags.static import static

from .forms import (
    GRASSCUTTING_RATE_PER_HOUR,
    ServiceRequestForm,
    ServiceRequestStep1Form,
    ServiceRequestStep2Form,
    ServiceRequestStep3Form,
    GrasscuttingApplicationForm,
    GrasscuttingAdminEditForm,
)
from .location import detect_barangay_for_point
from .geocode import address_in_bayawan, extract_barangay, reverse_geocode_osm
from .models import CompletionInfo, InspectionDetail, Notification, ServiceRequest, ServiceRequestChangeLog

# Session flag: verified "other person" consumer for service request wizard (step 2).
OTHER_VERIFIED_SESSION_KEY = "service_request_other_verified"


def _collapse_ws(s: str) -> str:
    return " ".join((s or "").split())


def _norm_key(s: str) -> str:
    return _collapse_ws(s).casefold()


def _other_verify_fingerprint(client_name, barangay, address) -> list[str]:
    return [
        _norm_key(client_name or ""),
        _norm_key(barangay or ""),
        _norm_key(address or ""),
    ]


def _addresses_compatible(form_addr: str, profile_street: str) -> bool:
    """Loose match between request address and ConsumerProfile.street_address."""
    fa = _norm_key(form_addr)
    pb = _norm_key(profile_street)
    if not fa or not pb:
        return False
    if len(fa) < 4 or len(pb) < 4:
        return fa == pb
    return fa == pb or fa in pb or pb in fa


def find_consumer_by_registered_profile(client_name, barangay, address):
    """
    Match an approved consumer User by full name + barangay + street address
    (as stored on ConsumerProfile). Returns (user|None, error_code|None).
    error_code: missing_name, missing_barangay, missing_address, none, multiple
    """
    name = _collapse_ws(client_name or "")
    if not name:
        return None, "missing_name"
    brgy = _collapse_ws(barangay or "")
    if not brgy:
        return None, "missing_barangay"
    addr = _collapse_ws(address or "")
    if not addr:
        return None, "missing_address"

    qs = User.objects.filter(
        role=User.Role.CONSUMER,
        is_active=True,
        is_approved=True,
        consumer_profile__isnull=False,
    ).select_related("consumer_profile")

    matches = []
    for u in qs.iterator():
        prof = u.consumer_profile
        fn = _collapse_ws(u.get_full_name())
        if _norm_key(fn) != _norm_key(name):
            continue
        if _norm_key(prof.barangay or "") != _norm_key(brgy):
            continue
        if _addresses_compatible(addr, prof.street_address or ""):
            matches.append(u)

    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "multiple"
    return None, "none"


def _consumer_other_verification_valid(request, client_name, barangay, address) -> bool:
    v = request.session.get(OTHER_VERIFIED_SESSION_KEY)
    if not v or not v.get("consumer_pk"):
        return False
    fp = _other_verify_fingerprint(client_name, barangay, address)
    return v.get("fp") == fp


def _clear_other_verification_if_stale(request, client_name, barangay, address) -> None:
    v = request.session.get(OTHER_VERIFIED_SESSION_KEY)
    if not v:
        return
    fp = _other_verify_fingerprint(client_name, barangay, address)
    if v.get("fp") != fp:
        request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)


def _owner_profile_dict(user):
    """Prefill payload for step 2 'I am the account owner' toggle."""
    data = {
        "client_name": user.get_full_name(),
        "contact_number": "",
        "address": "",
        "gps_latitude": None,
        "gps_longitude": None,
    }
    try:
        cp = user.consumer_profile
        data["contact_number"] = cp.mobile_number or ""
        data["address"] = cp.full_address or ""
        if cp.gps_latitude is not None:
            data["gps_latitude"] = float(cp.gps_latitude)
        if cp.gps_longitude is not None:
            data["gps_longitude"] = float(cp.gps_longitude)
    except Exception:
        pass
    return data


# ---------------------------------------------------------------------------
# Permission helper
# ---------------------------------------------------------------------------

def _register_xhtml2pdf_unicode_font():
    """Register a TTF so xhtml2pdf can render ₱ (U+20B1).

    xhtml2pdf resolves CSS font-family via its own fontList (copy of
    xhtml2pdf.default.DEFAULT_FONT), NOT only pdfmetrics. Registering TTFont
    alone is ignored — we must also add a lowercase key to DEFAULT_FONT.

    Bold table cells need addMapping(...) like xhtml2pdf's own TTF loader.
    """
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xhtml2pdf import default as x2p_default

    logical_name = "CenroPdfUnicode"
    full_name = f"{logical_name}_00"

    if full_name in pdfmetrics.getRegisteredFontNames():
        x2p_default.DEFAULT_FONT[str(logical_name).lower()] = logical_name
        return logical_name

    candidates = []
    bundled = os.path.join(settings.BASE_DIR, "static", "fonts", "DejaVuSans.ttf")
    if os.path.isfile(bundled):
        candidates.append(bundled)
    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates.extend(
            [
                os.path.join(windir, "Fonts", "segoeui.ttf"),
                os.path.join(windir, "Fonts", "seguisym.ttf"),
                os.path.join(windir, "Fonts", "arial.ttf"),
            ]
        )
    else:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf",
            ]
        )

    for font_path in candidates:
        if not font_path or not os.path.isfile(font_path):
            continue
        try:
            pdfmetrics.registerFont(TTFont(full_name, font_path))
            for bold in (0, 1):
                for italic in (0, 1):
                    addMapping(logical_name, bold, italic, full_name)
            x2p_default.DEFAULT_FONT[str(logical_name).lower()] = logical_name
            return logical_name
        except Exception:
            continue
    return None


def _can_act_on_request(user, service_request):
    """Return True if the user is the consumer, the requester, or an admin."""
    if user.is_admin():
        return True
    if service_request.consumer == user:
        return True
    if service_request.requested_by_id and service_request.requested_by_id == user.id:
        return True
    return False


# ---------------------------------------------------------------------------
# Multi-step service request wizard (3 steps)
# ---------------------------------------------------------------------------

@login_required
@role_required("CONSUMER")
@require_POST
def verify_other_consumer(request):
    """
    Check that client name + barangay + address match an approved consumer account.
    Used when "Requesting for another person" before continuing the wizard.
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = {}

    if data.get("reset"):
        request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)
        return JsonResponse({"ok": True, "cleared": True})

    name = data.get("client_name", "")
    brgy = data.get("barangay", "")
    addr = data.get("address", "")

    user_obj, err = find_consumer_by_registered_profile(name, brgy, addr)
    register_path = reverse("accounts:consumer_register")
    register_url = request.build_absolute_uri(register_path)

    messages_map = {
        "missing_name": "Enter the client's full name (as registered on EcoTrack).",
        "missing_barangay": "Set the service barangay (from the map or by typing it) before verifying.",
        "missing_address": (
            "Enter the street address as it appears on the client's EcoTrack profile "
            "(Complete Address / Landmark), then verify again."
        ),
        "none": (
            "No approved EcoTrack account matches this name, barangay, and address. "
            "The client must create an EcoTrack consumer account first so their record is on file; "
            "then you can continue this request."
        ),
        "multiple": (
            "More than one account matches. Ask the client to confirm their registered address, "
            "or contact CENRO for help."
        ),
    }

    if user_obj:
        fp = _other_verify_fingerprint(name, brgy, addr)
        request.session[OTHER_VERIFIED_SESSION_KEY] = {
            "consumer_pk": user_obj.pk,
            "fp": fp,
        }
        return JsonResponse({
            "ok": True,
            "message": (
                f"Registered account found for {user_obj.get_full_name()}. "
                "You can continue to the next step."
            ),
        })

    request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)
    return JsonResponse({
        "ok": False,
        "code": err or "none",
        "message": messages_map.get(err or "none", messages_map["none"]),
        "register_url": register_url,
    })


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

    if request.method == "GET" and step == 2:
        if form_data.get("request_for") != ServiceRequestStep2Form.REQUEST_FOR_OTHER:
            request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)

    form = None

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
            form = ServiceRequestStep2Form(
                request.POST,
                request.FILES,
                service_type=form_data.get("service_type"),
            )
            if form.is_valid():
                request_for_val = (
                    form.cleaned_data.get("request_for") or ServiceRequestStep2Form.REQUEST_FOR_OWNER
                )
                if request_for_val == ServiceRequestStep2Form.REQUEST_FOR_OWNER:
                    request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)
                elif not _consumer_other_verification_valid(
                    request,
                    form.cleaned_data.get("client_name"),
                    form.cleaned_data.get("barangay"),
                    form.cleaned_data.get("address"),
                ):
                    messages.error(
                        request,
                        'Please click "Verify client account" and confirm the client\'s name, barangay, and street '
                        "address match their registered EcoTrack profile before continuing.",
                    )
                    owner_profile = _owner_profile_dict(request.user)
                    return render(
                        request,
                        "services/create_request_wizard.html",
                        {
                            "form": form,
                            "step": 2,
                            "form_data": form_data,
                            "owner_profile_json": json.dumps(owner_profile),
                            "verify_other_consumer_url": reverse("services:verify_other_consumer"),
                            "consumer_register_url": reverse("accounts:consumer_register"),
                            "step2_other_verified": False,
                        },
                    )

                lat = form.cleaned_data.get("gps_latitude")
                lon = form.cleaned_data.get("gps_longitude")
                loc_mode = form.cleaned_data.get("location_mode") or "PIN"
                form_data.update({
                    "client_name": form.cleaned_data["client_name"],
                    "request_date": str(form.cleaned_data["request_date"]),
                    "contact_number": form.cleaned_data["contact_number"],
                    "location_mode": loc_mode,
                    "request_for": form.cleaned_data.get("request_for") or ServiceRequestStep2Form.REQUEST_FOR_OWNER,
                    "barangay": form.cleaned_data.get("barangay") or "",
                    "address": form.cleaned_data.get("address") or "",
                    "gps_latitude": float(lat) if lat is not None else None,
                    "gps_longitude": float(lon) if lon is not None else None,
                    "connected_to_bawad": form.cleaned_data["connected_to_bawad"],
                    "public_private": form.cleaned_data["public_private"],
                })
                if form.cleaned_data.get("bawad_proof"):
                    request.session["_bawad_proof_pending"] = True

                # Persist client signature (uploaded image or drawn on canvas) via a temporary file.
                sig_temp_path = None
                sig_file = request.FILES.get("client_signature")
                sig_data = (form.cleaned_data.get("client_signature_data") or "").strip()
                try:
                    if sig_data and sig_data.startswith("data:image"):
                        header, b64_data = sig_data.split(",", 1)
                        ext = ".png"
                        if "jpeg" in header or "jpg" in header:
                            ext = ".jpg"
                        elif "webp" in header:
                            ext = ".webp"
                        binary = base64.b64decode(b64_data)
                        path = f"client_signatures/temp/{uuid4()}{ext}"
                        default_storage.save(path, ContentFile(binary))
                        sig_temp_path = path
                    elif sig_file:
                        ext = os.path.splitext(sig_file.name)[1] or ".jpg"
                        if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                            ext = ".jpg"
                        path = f"client_signatures/temp/{uuid4()}{ext}"
                        default_storage.save(path, ContentFile(sig_file.read()))
                        sig_temp_path = path
                except Exception:
                    sig_temp_path = None

                if sig_temp_path:
                    form_data["_client_signature_path"] = sig_temp_path
                    request.session["_client_sig_pending"] = True
                else:
                    form_data.pop("_client_signature_path", None)
                    request.session.pop("_client_sig_pending", None)
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
                if form_data.get("service_type") == ServiceRequest.ServiceType.GRASS_CUTTING:
                    return HttpResponseRedirect(reverse("services:grasscutting_application"))
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

                # Determine which consumer account this request should belong to.
                target_consumer = request.user
                request_for = form_data.get("request_for") or ServiceRequestStep2Form.REQUEST_FOR_OWNER
                if request_for == ServiceRequestStep2Form.REQUEST_FOR_OTHER:
                    v = request.session.get(OTHER_VERIFIED_SESSION_KEY)
                    fp = _other_verify_fingerprint(
                        form_data.get("client_name"),
                        form_data.get("barangay"),
                        form_data.get("address"),
                    )
                    if not v or v.get("fp") != fp or not v.get("consumer_pk"):
                        messages.error(
                            request,
                            "Verification is missing or out of date. Please return to step 2 and verify the "
                            "client's registered account again.",
                        )
                        return redirect(reverse("services:create_request") + "?step=2")
                    try:
                        target_consumer = User.objects.get(
                            pk=v["consumer_pk"],
                            role=User.Role.CONSUMER,
                            is_active=True,
                        )
                    except User.DoesNotExist:
                        messages.error(
                            request,
                            "The verified account is no longer available. Please go back to step 2 and verify again.",
                        )
                        return redirect(reverse("services:create_request") + "?step=2")

                # Prevent duplicate active requests of the same type for the same owner.
                # Also expire stale inspection-fee pending requests so they don't block resubmission.
                try:
                    ServiceRequest.expire_pending_inspection_fees(days=7)
                except Exception:
                    pass
                existing_open = ServiceRequest.objects.filter(
                    consumer=target_consumer,
                    service_type=form_data.get("service_type", "RESIDENTIAL_DESLUDGING"),
                    status__in=[
                        ServiceRequest.Status.SUBMITTED,
                        ServiceRequest.Status.INSPECTION_FEE_DUE,
                        ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION,
                        ServiceRequest.Status.UNDER_REVIEW,
                        ServiceRequest.Status.INSPECTION_SCHEDULED,
                        ServiceRequest.Status.INSPECTED,
                        ServiceRequest.Status.COMPUTATION_SENT,
                        ServiceRequest.Status.AWAITING_PAYMENT,
                        ServiceRequest.Status.DESLUDGING_SCHEDULED,
                    ],
                ).exists()
                if existing_open:
                    messages.error(
                        request,
                        "You already have an ongoing request of this service type for this owner. "
                        "Please complete or cancel the existing request before submitting a new one.",
                    )
                    return redirect("services:history")

                service_request = ServiceRequest.objects.create(
                    consumer=target_consumer,
                    requested_by=request.user if target_consumer != request.user else None,
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

                # Determine if inspection fee is required (first-time desludging customer).
                is_desludging = service_request.service_type in [
                    ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
                    ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
                ]
                if is_desludging:
                    prior_desludging = ServiceRequest.objects.filter(
                        consumer=target_consumer,
                        service_type__in=[
                            ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
                            ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
                        ],
                        status__in=[
                            ServiceRequest.Status.INSPECTED,
                            ServiceRequest.Status.COMPLETED,
                        ],
                    ).exclude(pk=service_request.pk).exists()
                    if not prior_desludging:
                        service_request.status = ServiceRequest.Status.INSPECTION_FEE_DUE
                        service_request.save(update_fields=["status"])
                        Notification.objects.create(
                            user=service_request.consumer,
                            message=(
                                f"Request #{service_request.id} received. "
                                "Please pay the ₱150 inspection fee at the Treasurer's Office "
                                "and upload the receipt to continue."
                            ),
                            notification_type=Notification.NotificationType.STATUS_CHANGE,
                            related_request=service_request,
                        )

                # Attach client signature image, if any was captured in step 2.
                sig_path = form_data.get("_client_signature_path")
                if sig_path and default_storage.exists(sig_path):
                    try:
                        with default_storage.open(sig_path, "rb") as fp:
                            name = os.path.basename(sig_path)
                            service_request.client_signature.save(name, ContentFile(fp.read()), save=True)
                    except Exception:
                        pass
                    try:
                        default_storage.delete(sig_path)
                    except Exception:
                        pass

                # Notify all admins (role ADMIN, Django staff, or superusers)
                admin_users = User.objects.filter(
                    Q(role=User.Role.ADMIN) | Q(is_superuser=True) | Q(is_staff=True)
                )
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
                request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)

                messages.success(request, "Service request submitted successfully!")
                return render(request, "services/request_success.html", {
                    "reference_number": reference_number,
                    "service_request": service_request,
                })

    # GET or re-render with existing form
    if form is None:
        if step == 1:
            form = ServiceRequestStep1Form(initial={"service_type": form_data.get("service_type", "")})
        elif step == 2:
            default_contact = ""
            try:
                default_contact = request.user.consumer_profile.mobile_number or ""
            except Exception:
                pass
            form = ServiceRequestStep2Form(
                initial={
                    "client_name": form_data.get("client_name", request.user.get_full_name()),
                    "request_date": form_data.get("request_date", timezone.localdate().isoformat()),
                    "contact_number": form_data.get("contact_number", default_contact),
                    "location_mode": form_data.get("location_mode", "PIN"),
                    "request_for": form_data.get("request_for", ServiceRequestStep2Form.REQUEST_FOR_OWNER),
                    "connected_to_bawad": form_data.get("connected_to_bawad", "NO"),
                    "public_private": form_data.get("public_private", "PRIVATE"),
                    "barangay": form_data.get("barangay", ""),
                    "address": form_data.get("address", ""),
                    "gps_latitude": form_data.get("gps_latitude"),
                    "gps_longitude": form_data.get("gps_longitude"),
                },
                service_type=form_data.get("service_type"),
            )
        elif step == 3:
            form = ServiceRequestStep3Form()
        else:
            form = ServiceRequestStep1Form()
            step = 1

    owner_profile = {}
    verify_other_consumer_url = ""
    consumer_register_url = ""
    step2_other_verified = False
    if step == 2:
        owner_profile = _owner_profile_dict(request.user)
        verify_other_consumer_url = reverse("services:verify_other_consumer")
        consumer_register_url = reverse("accounts:consumer_register")
        step2_other_verified = _consumer_other_verification_valid(
            request,
            form_data.get("client_name"),
            form_data.get("barangay"),
            form_data.get("address"),
        )

    context = {
        "form": form,
        "step": step,
        "form_data": form_data,
        "owner_profile_json": json.dumps(owner_profile),
        "verify_other_consumer_url": verify_other_consumer_url,
        "consumer_register_url": consumer_register_url,
        "step2_other_verified": step2_other_verified,
    }
    return render(request, "services/create_request_wizard.html", context)


# ---------------------------------------------------------------------------
# Grasscutting Services Application Form (only for Grass Cutting flow)
# ---------------------------------------------------------------------------

@login_required
@role_required("CONSUMER")
def grasscutting_application(request):
    """Show and process the Grasscutting Services Application Form. Only reachable after step 2 when service is Grass Cutting."""
    form_data = request.session.get("service_request_data", {})
    if form_data.get("service_type") != ServiceRequest.ServiceType.GRASS_CUTTING:
        messages.warning(request, "Please start a Grass Cutting service request first.")
        return redirect("services:create_request")

    if form_data.get("request_for") == ServiceRequestStep2Form.REQUEST_FOR_OTHER:
        if not _consumer_other_verification_valid(
            request,
            form_data.get("client_name"),
            form_data.get("barangay"),
            form_data.get("address"),
        ):
            messages.error(
                request,
                "Client verification is missing or out of date. Please complete step 2 and verify the "
                "registered EcoTrack account before continuing.",
            )
            return redirect(reverse("services:create_request") + "?step=2")

    initial = {
        "signature_over_printed_name": form_data.get("client_name", request.user.get_full_name()),
        "contact_number": form_data.get("contact_number", ""),
        "address": (form_data.get("address") or "").strip() or (form_data.get("barangay") or "").strip(),
    }
    if form_data.get("request_date"):
        try:
            initial["date"] = form_data["request_date"]
            initial["date_of_grass_cutting"] = form_data["request_date"]
        except Exception:
            pass

    if request.method == "POST":
        form = GrasscuttingApplicationForm(request.POST, initial=initial)
        if form.is_valid():
            cd = form.cleaned_data
            personnel = cd["number_of_personnel"]
            hours = float(cd["number_of_hours"])
            # Always compute on backend; do not trust any client-submitted total.
            total_amount = personnel * hours * GRASSCUTTING_RATE_PER_HOUR
            total_amount = max(0, round(total_amount, 2))

            designated_time_val = cd["designated_time"]
            if hasattr(designated_time_val, "strftime"):
                designated_time_str = designated_time_val.strftime("%I:%M %p")
            else:
                designated_time_str = str(designated_time_val)

            notes_lines = [
                "GRASSCUTTING APPLICATION",
                f"Date: {cd['date']}",
                f"Date of Grass Cutting: {cd['date_of_grass_cutting']}",
                f"Designated Time: {designated_time_str}",
                f"Place of Grass Cutting: {cd['place_of_grass_cutting']}",
                f"Signature over printed name: {cd['signature_over_printed_name']}",
                f"Contact Number: {cd['contact_number']}",
                f"Address: {cd['address']}",
                f"Number of Personnel: {personnel}",
                f"Number of Hours: {hours}",
                f"Rate: ₱{GRASSCUTTING_RATE_PER_HOUR}/hour per personnel",
                f"Total Amount: ₱{total_amount:.2f}",
            ]
            notes = "\n".join(notes_lines)

            gps_latitude = None
            gps_longitude = None
            if form_data.get("gps_latitude") is not None:
                try:
                    gps_latitude = Decimal(str(form_data["gps_latitude"]))
                except (ValueError, TypeError):
                    pass
            if form_data.get("gps_longitude") is not None:
                try:
                    gps_longitude = Decimal(str(form_data["gps_longitude"]))
                except (ValueError, TypeError):
                    pass

            req_date = (
                datetime.strptime(form_data["request_date"], "%Y-%m-%d").date()
                if form_data.get("request_date")
                else timezone.now().date()
            )

            target_consumer = request.user
            requested_by_user = None
            if form_data.get("request_for") == ServiceRequestStep2Form.REQUEST_FOR_OTHER:
                v = request.session.get(OTHER_VERIFIED_SESSION_KEY)
                fp = _other_verify_fingerprint(
                    form_data.get("client_name"),
                    form_data.get("barangay"),
                    form_data.get("address"),
                )
                if not v or v.get("fp") != fp or not v.get("consumer_pk"):
                    messages.error(
                        request,
                        "Verification is missing or out of date. Please return to step 2 and verify again.",
                    )
                    return redirect(reverse("services:create_request") + "?step=2")
                try:
                    target_consumer = User.objects.get(
                        pk=v["consumer_pk"],
                        role=User.Role.CONSUMER,
                        is_active=True,
                    )
                except User.DoesNotExist:
                    messages.error(request, "Verified account is no longer available. Please verify again on step 2.")
                    return redirect(reverse("services:create_request") + "?step=2")
                requested_by_user = request.user

            # Also expire stale inspection-fee pending requests so they don't block resubmission.
            try:
                ServiceRequest.expire_pending_inspection_fees(days=7)
            except Exception:
                pass

            existing_open = ServiceRequest.objects.filter(
                consumer=target_consumer,
                service_type=ServiceRequest.ServiceType.GRASS_CUTTING,
                status__in=[
                    ServiceRequest.Status.SUBMITTED,
                    ServiceRequest.Status.INSPECTION_FEE_DUE,
                    ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION,
                    ServiceRequest.Status.UNDER_REVIEW,
                    ServiceRequest.Status.INSPECTION_SCHEDULED,
                    ServiceRequest.Status.INSPECTED,
                    ServiceRequest.Status.COMPUTATION_SENT,
                    ServiceRequest.Status.AWAITING_PAYMENT,
                    ServiceRequest.Status.DESLUDGING_SCHEDULED,
                ],
            ).exists()
            if existing_open:
                messages.error(
                    request,
                    "There is already an open Grass Cutting request for this owner. "
                    "Please complete or wait for it to finish before submitting another.",
                )
                return redirect("services:history")

            service_request = ServiceRequest.objects.create(
                consumer=target_consumer,
                requested_by=requested_by_user,
                client_name=form_data.get("client_name", request.user.get_full_name()),
                request_date=req_date,
                contact_number=cd["contact_number"],
                location_mode=form_data.get("location_mode", "PIN"),
                barangay=form_data.get("barangay", ""),
                address=cd["address"],
                gps_latitude=gps_latitude,
                gps_longitude=gps_longitude,
                service_type=ServiceRequest.ServiceType.GRASS_CUTTING,
                connected_to_bawad=False,
                public_private=form_data.get("public_private", "PRIVATE"),
                status=ServiceRequest.Status.SUBMITTED,
                notes=notes,
                fee_amount=Decimal(str(round(total_amount, 2))),
                grasscutting_date=cd["date_of_grass_cutting"],
                grasscutting_personnel=personnel,
                grasscutting_hours=Decimal(str(hours)),
            )

            admin_users = User.objects.filter(
                Q(role=User.Role.ADMIN) | Q(is_superuser=True) | Q(is_staff=True)
            )
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"New Grass Cutting request from {service_request.client_name}.",
                    notification_type=Notification.NotificationType.REQUEST_SUBMITTED,
                    related_request=service_request,
                )
            Notification.objects.create(
                user=request.user,
                message="Your Grass Cutting request has been submitted.",
                notification_type=Notification.NotificationType.STATUS_CHANGE,
                related_request=service_request,
            )

            request.session.pop("service_request_data", None)
            request.session.pop("_bawad_proof_pending", None)
            request.session.pop("_client_sig_pending", None)
            request.session.pop("_location_photos", None)
            request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)

            messages.success(request, "Grasscutting application submitted successfully!")
            return render(request, "services/request_success.html", {
                "reference_number": f"ECO-{timezone.now().year}-{service_request.id % 1000:03d}",
                "service_request": service_request,
            })
    else:
        form = GrasscuttingApplicationForm(initial=initial)

    return render(request, "services/grasscutting_application.html", {
        "form": form,
        "form_data": form_data,
        "rate_per_hour": GRASSCUTTING_RATE_PER_HOUR,
    })


def _parse_grasscutting_notes(notes_str):
    """Parse notes text for grasscutting date, personnel, hours (backfill when DB fields are null)."""
    if not notes_str:
        return None, None, None
    date_val = None
    personnel_val = None
    hours_val = None
    for line in notes_str.splitlines():
        line = line.strip()
        if line.startswith("Date of Grass Cutting:"):
            part = line.split(":", 1)[-1].strip()
            if part:
                try:
                    from datetime import datetime as dt
                    date_val = dt.strptime(part, "%Y-%m-%d").date()
                except Exception:
                    pass
        elif line.startswith("Number of Personnel:"):
            part = line.split(":", 1)[-1].strip()
            if part:
                try:
                    personnel_val = int(part)
                except ValueError:
                    pass
        elif line.startswith("Number of Hours:"):
            part = line.split(":", 1)[-1].strip()
            if part:
                try:
                    hours_val = Decimal(part)
                except Exception:
                    pass
    return date_val, personnel_val, hours_val


def _extract_grasscutting_field(notes_str, label_prefix: str) -> str:
    """Return the value after a given 'Label: ' prefix in the notes, or empty string."""
    if not notes_str:
        return ""
    for line in notes_str.splitlines():
        line = line.strip()
        if line.startswith(label_prefix):
            return line.split(":", 1)[-1].strip()
    return ""


@login_required
@role_required("ADMIN", "STAFF")
def grasscutting_request_detail(request, pk):
    """Admin view: show Grasscutting Application details and allow editing date, personnel, hours (with required remarks)."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING:
        messages.error(request, "This page is only for Grass Cutting requests.")
        return redirect("services:request_detail", pk=pk)

    # Resolve display values (from DB or backfill from notes)
    g_date = service_request.grasscutting_date
    g_personnel = service_request.grasscutting_personnel
    g_hours = service_request.grasscutting_hours
    if g_date is None or g_personnel is None or g_hours is None:
        parsed_date, parsed_personnel, parsed_hours = _parse_grasscutting_notes(service_request.notes)
        if g_date is None:
            g_date = parsed_date
        if g_personnel is None:
            g_personnel = parsed_personnel
        if g_hours is None:
            g_hours = parsed_hours

    rate_per_hour = GRASSCUTTING_RATE_PER_HOUR
    designated_time = _extract_grasscutting_field(service_request.notes, "Designated Time:")
    place_of_grass_cutting = _extract_grasscutting_field(service_request.notes, "Place of Grass Cutting:")
    initial = {
        "date_of_grass_cutting": g_date,
        "number_of_personnel": g_personnel or 1,
        "number_of_hours": g_hours or Decimal("1"),
        "remarks": "",
    }

    if request.method == "POST":
        form = GrasscuttingAdminEditForm(request.POST, initial=initial)
        if form.is_valid():
            cd = form.cleaned_data
            new_date = cd["date_of_grass_cutting"]
            new_personnel = cd["number_of_personnel"]
            posted_hours = cd["number_of_hours"]
            remarks = (cd["remarks"] or "").strip()
            # Enforce that admin cannot change number_of_hours
            if g_hours is not None and posted_hours != g_hours:
                form.add_error("number_of_hours", "Number of hours cannot be modified by admin.")
            if not remarks:
                form.add_error("remarks", "Remarks are required.")

            if not form.errors:
                new_hours = g_hours or posted_hours
                old_values = {}
                new_values = {}
                if g_date != new_date:
                    old_values["date_of_grass_cutting"] = str(g_date) if g_date else None
                    new_values["date_of_grass_cutting"] = str(new_date)
                if g_personnel != new_personnel:
                    old_values["number_of_personnel"] = g_personnel
                    new_values["number_of_personnel"] = new_personnel

                if not old_values and not new_values:
                    messages.info(request, "No changes were made.")
                else:
                    # Update request (hours are kept from original; only personnel/date may change)
                    service_request.grasscutting_date = new_date
                    service_request.grasscutting_personnel = new_personnel
                    service_request.grasscutting_hours = new_hours
                    total = new_personnel * float(new_hours) * rate_per_hour
                    service_request.fee_amount = Decimal(str(round(total, 2)))
                    service_request.save(update_fields=["grasscutting_date", "grasscutting_personnel", "grasscutting_hours", "fee_amount", "updated_at"])

                    # Audit log
                    ServiceRequestChangeLog.objects.create(
                        service_request=service_request,
                        changed_by=request.user,
                        remarks=remarks,
                        old_values=old_values,
                        new_values=new_values,
                    )

                    # Notify consumer (include remarks and what changed)
                    change_parts = []
                    if "date_of_grass_cutting" in new_values:
                        change_parts.append(f"Date of Grass Cutting: {old_values.get('date_of_grass_cutting', '—')} → {new_values['date_of_grass_cutting']}")
                    if "number_of_personnel" in new_values:
                        change_parts.append(f"Number of Personnel: {old_values.get('number_of_personnel', '—')} → {new_values['number_of_personnel']}")
                    changes_text = "; ".join(change_parts)
                    msg = f"Your Grass Cutting request was updated. Reason: {remarks}"
                    if changes_text:
                        msg += f" Changes: {changes_text}"
                    if len(msg) > 500:
                        msg = msg[:497] + "..."
                    Notification.objects.create(
                        user=service_request.consumer,
                        message=msg,
                        notification_type=Notification.NotificationType.STATUS_CHANGE,
                        related_request=service_request,
                    )
                    messages.success(request, "Changes saved. The consumer has been notified.")
                return redirect("services:grasscutting_request_detail", pk=pk)
    else:
        form = GrasscuttingAdminEditForm(initial=initial)

    # Build display summary from notes (full text) for read-only section
    return render(request, "services/grasscutting_request_detail.html", {
        "sr": service_request,
        "form": form,
        "grasscutting_date": g_date,
        "grasscutting_personnel": g_personnel,
        "grasscutting_hours": g_hours,
        "designated_time": designated_time,
        "place_of_grass_cutting": place_of_grass_cutting,
        "rate_per_hour": rate_per_hour,
        "total_amount": service_request.fee_amount,
        "change_logs": service_request.change_logs.all()[:20],
    })


@login_required
def grasscutting_request_view(request, pk):
    """Consumer view: read-only Grasscutting Application Form + admin changes."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING:
        messages.error(request, "This page is only for Grass Cutting requests.")
        return redirect("services:request_detail", pk=pk)

    # Permission: owner (consumer) or admin/staff
    is_owner = service_request.consumer == request.user
    is_admin_like = hasattr(request.user, "is_admin") and request.user.is_admin() or hasattr(request.user, "is_staff_member") and request.user.is_staff_member()
    if not (is_owner or is_admin_like):
        messages.error(request, "You do not have permission to view this grasscutting form.")
        return redirect("services:request_list")

    # Resolve stored values or backfill from notes
    g_date = service_request.grasscutting_date
    g_personnel = service_request.grasscutting_personnel
    g_hours = service_request.grasscutting_hours
    if g_date is None or g_personnel is None or g_hours is None:
        parsed_date, parsed_personnel, parsed_hours = _parse_grasscutting_notes(service_request.notes)
        if g_date is None:
            g_date = parsed_date
        if g_personnel is None:
            g_personnel = parsed_personnel
        if g_hours is None:
            g_hours = parsed_hours

    rate_per_hour = GRASSCUTTING_RATE_PER_HOUR
    designated_time = _extract_grasscutting_field(service_request.notes, "Designated Time:")
    place_of_grass_cutting = _extract_grasscutting_field(service_request.notes, "Place of Grass Cutting:")
    total_amount = service_request.fee_amount
    if total_amount is None and g_personnel and g_hours:
        total_amount = Decimal(str(round(g_personnel * float(g_hours) * rate_per_hour, 2)))

    change_logs = service_request.change_logs.all()[:20]

    return render(request, "services/grasscutting_request_view.html", {
        "sr": service_request,
        "grasscutting_date": g_date,
        "grasscutting_personnel": g_personnel,
        "grasscutting_hours": g_hours,
        "designated_time": designated_time,
        "place_of_grass_cutting": place_of_grass_cutting,
        "rate_per_hour": rate_per_hour,
        "total_amount": total_amount,
        "change_logs": change_logs,
    })


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


def _infer_barangay_from_display_name(display_name: str | None) -> str | None:
    """
    Try to infer a barangay / village / sitio name from the human‑readable
    display_name coming from OSM.

    Heuristics:
    - If we find a segment that clearly represents the city "Bayawan" (e.g.
      "Bayawan", "Bayawan City", "City of Bayawan"), we take the segment
      immediately before it as the barangay.
    - Otherwise we start from the first segment, but if it looks like a
      road or facility name ("Dumaguete South Road", "National High School",
      "Government Center", etc.), we instead fall back to the second segment
      which is usually the barangay / sitio.
    """
    if not display_name:
        return None
    try:
        parts = [p.strip() for p in display_name.split(",") if p.strip()]
        if not parts:
            return None

        def is_bayawan_city_token(token: str) -> bool:
            s = token.strip().lower()
            return s in {"bayawan", "bayawan city", "city of bayawan"}

        city_idx = None
        for i, part in enumerate(parts):
            if is_bayawan_city_token(part):
                city_idx = i
                break

        if city_idx is not None and city_idx > 0:
            candidate = parts[city_idx - 1]
        else:
            # Start from the first segment but skip over obvious
            # establishments / facilities and street names.
            def looks_like_facility_or_road(token: str) -> bool:
                lower = token.lower()
                skip_keywords = [
                    "road",
                    "rd",
                    "street",
                    "st.",
                    "st ",
                    "highway",
                    "hwy",
                    "gov't",
                    "government",
                    "center",
                    "centre",
                    "school",
                    "college",
                    "university",
                    "hall",
                    "church",
                    "mall",
                    "market",
                    "bridge",
                    "monument",
                    "park",
                    "playground",
                    "sports",
                    "complex",
                    "resort",
                    "restaurant",
                    "kubo",
                ]
                return any(kw in lower for kw in skip_keywords)

            candidate = parts[0]
            if looks_like_facility_or_road(candidate) and len(parts) > 1:
                candidate = parts[1]
                # If the second segment is *also* a road/facility (e.g.
                # "Bahay Kubo, Dumaguete South Road, Poblacion, ..."),
                # then fall back to the third segment which is usually
                # the actual barangay like "Poblacion".
                if looks_like_facility_or_road(candidate) and len(parts) > 2:
                    candidate = parts[2]

        candidate = candidate.strip()
        if candidate and candidate.lower() not in {"city", "municipality"}:
            return candidate
    except Exception:
        return None
    return None


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
        display_name = cached.get("display_name")
        within_bayawan = bool(detected) or bool(cached.get("within_bayawan"))

        # Start from freshest detected / previously stored barangay
        barangay_base = detected or cached.get("barangay")
        inferred = _infer_barangay_from_display_name(display_name)
        # Always prefer the more specific inferred barangay when available
        barangay = inferred or barangay_base

        cached["within_bayawan"] = within_bayawan
        cached["barangay"] = barangay
        return JsonResponse(cached)

    data = reverse_geocode_osm(lat_f, lon_f)
    if not data:
        return JsonResponse({"ok": False, "error": "Reverse geocoding failed"}, status=502)

    address = data.get("address") or {}
    display_name = data.get("display_name")

    within_bayawan = bool(detected) or address_in_bayawan(address, display_name)
    barangay_base = detected or extract_barangay(address)
    inferred = _infer_barangay_from_display_name(display_name)
    barangay = inferred or barangay_base

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
    # Cleanup before listing requests (keeps the workflow from accumulating stale pending items).
    try:
        ServiceRequest.expire_pending_inspection_fees(days=7)
    except Exception:
        pass

    if request.user.is_admin():
        requests_qs = ServiceRequest.objects.all().select_related("consumer").order_by("-created_at")
    elif request.user.is_staff_member():
        requests_qs = ServiceRequest.objects.filter(
            assigned_inspector=request.user
        ).select_related("consumer").order_by("-created_at")
    else:
        # Customers: show their own requests plus any in‑progress requests
        # they submitted on behalf of another account.
        requests_qs = ServiceRequest.objects.filter(
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
                ],
            )
        ).order_by("-created_at")
    return render(request, "services/request_list.html", {"requests": requests_qs})


@login_required
def request_detail(request, pk):
    # Cleanup so customers/admins see the latest status (e.g., inspection-fee expired).
    try:
        ServiceRequest.expire_pending_inspection_fees(days=7)
    except Exception:
        pass

    service_request = get_object_or_404(ServiceRequest, pk=pk)
    is_admin = request.user.is_admin()
    is_staff = request.user.is_staff_member()
    is_admin_like = is_admin or is_staff

    # Consumers: can view their own requests plus any request they submitted
    # on behalf of another person (requested_by), at all statuses.
    if not is_admin_like and not _can_act_on_request(request.user, service_request):
        messages.error(request, "You do not have permission to view this request.")
        return redirect("services:request_list")

    # Staff: can only view requests assigned to them.
    if is_staff and service_request.assigned_inspector_id != request.user.id:
        messages.error(request, "You can only view requests assigned to you.")
        return redirect("services:request_list")

    # When an admin/staff opens a newly submitted request, automatically move it to "Under Review".
    if is_admin_like and service_request.status == ServiceRequest.Status.SUBMITTED:
        service_request.status = ServiceRequest.Status.UNDER_REVIEW
        service_request.save(update_fields=["status", "updated_at"])
        try:
            Notification.objects.create(
                user=service_request.consumer,
                message=f"Your service request #{service_request.id} is now under review.",
                notification_type=Notification.NotificationType.STATUS_CHANGE,
                related_request=service_request,
            )
        except Exception:
            pass

    staff_members = User.objects.filter(role__in=[User.Role.ADMIN, User.Role.STAFF])

    has_prior_inspected = False
    can_assign_inspector = False
    inspection_optional = False
    inspection_waived = False

    if is_admin_like:
        from .models import ServiceRequest as SRModel

        prior_qs = SRModel.objects.filter(
            consumer=service_request.consumer,
            service_type=service_request.service_type,
        ).exclude(pk=service_request.pk)
        has_prior_inspected = prior_qs.filter(
            status__in=[
                SRModel.Status.INSPECTION_SCHEDULED,
                SRModel.Status.INSPECTED,
                SRModel.Status.COMPLETED,
            ]
        ).exists()

        can_assign_inspector = (
            service_request.status
            in [SRModel.Status.UNDER_REVIEW, SRModel.Status.SUBMITTED]
            and not has_prior_inspected
        )
        inspection_optional = (
            service_request.status
            in [SRModel.Status.UNDER_REVIEW, SRModel.Status.SUBMITTED]
            and has_prior_inspected
        )
        inspection_waived = "[NO_INSPECTION_FEE]" in (service_request.notes or "")

    # Keep computation payment_status in sync with request state (receipt uploaded / confirmed).
    computation = getattr(service_request, "computation", None)
    if computation:
        from dashboard.models import ServiceComputation
        if service_request.status == ServiceRequest.Status.AWAITING_PAYMENT and service_request.treasurer_receipt:
            if computation.payment_status != ServiceComputation.PaymentStatus.FREE and computation.payment_status != ServiceComputation.PaymentStatus.AWAITING_VERIFICATION:
                computation.payment_status = ServiceComputation.PaymentStatus.AWAITING_VERIFICATION
                computation.save(update_fields=["payment_status"])
        elif service_request.status == ServiceRequest.Status.PAID:
            if computation.payment_status != ServiceComputation.PaymentStatus.FREE and computation.payment_status != ServiceComputation.PaymentStatus.PAID:
                computation.payment_status = ServiceComputation.PaymentStatus.PAID
                computation.save(update_fields=["payment_status"])

    # Derive inspector label + time (from notes) for display.
    inspector_label = ""
    inspection_time = ""
    inspection_reason = ""
    desludging_time = ""
    if service_request.notes:
        marker = "Inspection scheduled with "
        idx = service_request.notes.rfind(marker)
        if idx != -1:
            segment = service_request.notes[idx + len(marker):]
            # Expected: "Inspector X on YYYY-MM-DD at HH:MM AM. Reason: ..."
            try:
                name_part, rest = segment.split(" on ", 1)
                inspector_label = name_part.strip()
                if " at " in rest:
                    _, time_part = rest.split(" at ", 1)
                    # Separate optional "Reason: ..." from time string
                    if "Reason:" in time_part:
                        time_only, reason_part = time_part.split("Reason:", 1)
                        inspection_time = time_only.strip().rstrip(".")
                        inspection_reason = reason_part.strip().rstrip(".")
                    else:
                        inspection_time = time_part.strip().rstrip(".")
            except ValueError:
                pass

        # Also try to extract last recorded desludging time from notes.
        dl_marker = "Desludging scheduled on "
        dl_idx = service_request.notes.rfind(dl_marker)
        if dl_idx != -1:
            dl_segment = service_request.notes[dl_idx + len(dl_marker):]
            # Expected: "YYYY-MM-DD at HH:MM AM." or "YYYY-MM-DD at HH:MM AM. Reason: ..."
            try:
                if " at " in dl_segment:
                    _, time_part = dl_segment.split(" at ", 1)
                    if "Reason:" in time_part:
                        time_only, _ = time_part.split("Reason:", 1)
                        desludging_time = time_only.strip().rstrip(".")
                    else:
                        desludging_time = time_part.strip().rstrip(".")
            except ValueError:
                pass

    context = {
        "sr": service_request,
        "staff_members": staff_members,
        "can_assign_inspector": can_assign_inspector,
        "inspection_optional": inspection_optional,
        "inspection_waived": inspection_waived,
        "has_prior_inspected": has_prior_inspected,
        "inspector_label": inspector_label,
        "inspection_time": inspection_time,
        "inspection_reason": inspection_reason,
        "desludging_time": desludging_time,
    }
    return render(request, "services/request_detail.html", context)


@login_required
def history(request):
    if request.user.is_admin():
        requests_qs = ServiceRequest.objects.all().select_related("consumer").order_by("-created_at")
    elif request.user.is_staff_member():
        requests_qs = ServiceRequest.objects.filter(
            assigned_inspector=request.user
        ).select_related("consumer").order_by("-created_at")
    else:
        # For regular customers, show:
        #  - all requests where they are the consumer
        #  - all requests they submitted on behalf of another person (requested_by)
        requests_qs = ServiceRequest.objects.filter(
            Q(consumer=request.user) | Q(requested_by=request.user)
        ).order_by("-created_at")
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
    # Once inspection is submitted, only admins may make further changes.
    if hasattr(request.user, "is_staff_member") and request.user.is_staff_member():
        if service_request.status != ServiceRequest.Status.INSPECTION_SCHEDULED:
            messages.error(request, "You can only fill inspection details once. Further changes are handled by an admin.")
            return redirect("services:request_detail", pk=pk)
    if request.method == "POST":
        InspectionDetail.objects.update_or_create(
            service_request=service_request,
            defaults={
                "inspection_date": request.POST.get("inspection_date"),
                "inspected_by": request.POST.get("inspected_by", ""),
                "remarks": request.POST.get("remarks", ""),
            },
        )

        # Optionally capture basic completion info at the same time as inspection.
        raw_m3 = (request.POST.get("cubic_meters") or "").strip()
        if raw_m3:
            try:
                service_request.cubic_meters = Decimal(raw_m3)
                service_request.save(update_fields=["cubic_meters"])
            except Exception:
                pass

        completion_defaults = {
            "date_completed": timezone.now().date(),
            "time_required": "N/A",
            "witnessed_by_name": request.POST.get("witnessed_by_name", ""),
            "declogger_no": "",
            "driver_name": "",
            "helper1_name": "",
            "helper2_name": "",
            "helper3_name": "",
        }
        completion, _ = CompletionInfo.objects.update_or_create(
            service_request=service_request,
            defaults=completion_defaults,
        )

        # Witness signature: upload, camera, or drawn on screen (data URL)
        sig_data = (request.POST.get("witness_signature_data") or "").strip()
        sig_file = request.FILES.get("witnessed_by_signature")
        try:
            if sig_data and sig_data.startswith("data:image"):
                header, b64_data = sig_data.split(",", 1)
                ext = ".png"
                if "jpeg" in header or "jpg" in header:
                    ext = ".jpg"
                elif "webp" in header:
                    ext = ".webp"
                binary = base64.b64decode(b64_data)
                filename = f"witness-sign-{service_request.id}-{int(time.time())}{ext}"
                completion.witnessed_by_signature.save(
                    filename, ContentFile(binary), save=True
                )
            elif sig_file:
                completion.witnessed_by_signature.save(
                    sig_file.name, sig_file, save=True
                )
        except Exception:
            pass

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

    # Reuse parsed inspector label and time from request_detail for defaults.
    inspector_label = ""
    inspection_time = ""
    if service_request.notes:
        marker = "Inspection scheduled with "
        idx = service_request.notes.rfind(marker)
        if idx != -1:
            segment = service_request.notes[idx + len(marker):]
            try:
                name_part, rest = segment.split(" on ", 1)
                inspector_label = name_part.strip()
                if " at " in rest:
                    _, time_part = rest.split(" at ", 1)
                    if "Reason:" in time_part:
                        time_only, _ = time_part.split("Reason:", 1)
                        inspection_time = time_only.strip().rstrip(".")
                    else:
                        inspection_time = time_part.strip().rstrip(".")
            except ValueError:
                pass

    context = {
        "sr": service_request,
        "inspector_label": inspector_label or "Inspector 1",
        "inspection_time": inspection_time,
    }
    return render(request, "services/inspection_form.html", context)


@login_required
@role_required("ADMIN")
def submit_completion(request, pk):
    """Admin/Staff fills completion info, triggers computation generation."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if request.method == "POST":
        # Save cubic meters from completion form onto the service request
        raw_m3 = (request.POST.get("cubic_meters") or "").strip()
        if raw_m3:
            try:
                service_request.cubic_meters = Decimal(raw_m3)
                service_request.save(update_fields=["cubic_meters"])
            except Exception:
                # Ignore invalid input; downstream logic will fall back to defaults
                pass

        existing_completion = getattr(service_request, "completion_info", None)
        witnessed_name_post = (request.POST.get("witnessed_by_name") or "").strip()
        witnessed_name = witnessed_name_post or (
            existing_completion.witnessed_by_name if existing_completion else ""
        )

        # Base completion info (date/time now auto-handled server-side)
        completion, _ = CompletionInfo.objects.update_or_create(
            service_request=service_request,
            defaults={
                "date_completed": timezone.now().date(),
                "time_required": "N/A",
                "witnessed_by_name": witnessed_name,
                "declogger_no": request.POST.get("declogger_no", ""),
                "driver_name": request.POST.get("driver_name", ""),
                "helper1_name": request.POST.get("helper1_name", ""),
                "helper2_name": request.POST.get("helper2_name", ""),
                "helper3_name": request.POST.get("helper3_name", ""),
            },
        )

        # Witness signature: upload, camera, or drawn on screen (data URL)
        sig_data = (request.POST.get("witness_signature_data") or "").strip()
        sig_file = request.FILES.get("witnessed_by_signature")
        try:
            if sig_data and sig_data.startswith("data:image"):
                header, b64_data = sig_data.split(",", 1)
                ext = ".png"
                if "jpeg" in header or "jpg" in header:
                    ext = ".jpg"
                elif "webp" in header:
                    ext = ".webp"
                binary = base64.b64decode(b64_data)
                completion.witnessed_by_signature.save(
                    f"witness_signatures/{service_request.id}{ext}",
                    ContentFile(binary),
                    save=False,
                )
            elif sig_file:
                completion.witnessed_by_signature = sig_file
        except Exception:
            # If anything goes wrong with signature processing, just skip saving it.
            pass

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
    # Mark as draft; admin will finalize and send to customer separately.
    comp.is_finalized = False
    comp.save()

    service_request.fee_amount = comp.total_charge
    service_request.save(update_fields=["fee_amount"])


def _match_other_consumer_account(form_data):
    """
    Legacy helper: resolve consumer from name + barangay + address (same rules as Verify).
    """
    user_obj, _err = find_consumer_by_registered_profile(
        form_data.get("client_name"),
        form_data.get("barangay"),
        form_data.get("address"),
    )
    return user_obj


# ---------------------------------------------------------------------------
# Computation letter view
# ---------------------------------------------------------------------------

@login_required
def view_computation(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    # Only admin, consumer (owner), or the account that submitted the request may view. Staff/inspector may not.
    can_view = (
        request.user.is_admin()
        or service_request.consumer == request.user
        or (service_request.requested_by and service_request.requested_by == request.user)
    )
    if not can_view:
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")

    computation = getattr(service_request, "computation", None)
    if not computation:
        messages.warning(request, "Computation not yet available.")
        return redirect("services:request_detail", pk=pk)

    # Allow admins/staff to post from this page to finalize and send.
    if request.method == "POST":
        if not (request.user.is_admin() or request.user.is_staff_member()):
            messages.error(request, "Permission denied.")
            return redirect("services:request_detail", pk=pk)
        action = request.POST.get("action") or "finalize"
        if action == "finalize":
            sig = request.FILES.get("prepared_by_signature")
            if sig:
                computation.prepared_by_signature = sig
            computation.prepared_by = request.user
            computation.is_finalized = True
            computation.finalized_at = timezone.now()
            computation.save()

            service_request.status = ServiceRequest.Status.COMPUTATION_SENT
            service_request.fee_amount = computation.total_charge
            service_request.save(update_fields=["status", "fee_amount"])

            # Notify the account owner (consumer) and, if different, the account that submitted the request.
            Notification.objects.create(
                user=service_request.consumer,
                message="Your computation letter is ready. You can now view and download it.",
                notification_type=Notification.NotificationType.COMPUTATION_READY,
                related_request=service_request,
            )
            if service_request.requested_by and service_request.requested_by != service_request.consumer:
                Notification.objects.create(
                    user=service_request.requested_by,
                    message="Your computation letter is ready. You can now view and download it.",
                    notification_type=Notification.NotificationType.COMPUTATION_READY,
                    related_request=service_request,
                )
            messages.success(request, "Computation finalized, signed, and sent to the customer.")
            return redirect("services:view_computation", pk=pk)

    # Keep personnel_count in sync with completion info so Meals & Transportation matches
    completion = getattr(service_request, "completion_info", None)
    if completion:
        desired_personnel = completion.personnel_count
        if computation.personnel_count != desired_personnel:
            computation.personnel_count = desired_personnel
            computation.save()

    # Only admin may see unfinalized computation; consumer and requested_by see it only after finalization.
    if not request.user.is_admin() and not computation.is_finalized:
        messages.info(request, "The computation is still being finalized by the administrator.")
        return redirect("services:request_detail", pk=pk)

    # Address without repeating barangay if it already appears at the end
    addr = (service_request.address or "").strip()
    brgy = (service_request.barangay or "").strip()
    if brgy and addr.endswith(brgy):
        address_display = addr
    else:
        address_display = f"{addr}, {brgy}" if brgy else addr

    # Absolute URL for signature image so it displays reliably on the letter (and when printing)
    prepared_by_signature_url = None
    if computation.prepared_by_signature:
        prepared_by_signature_url = request.build_absolute_uri(computation.prepared_by_signature.url)

    return render(request, "services/computation_letter.html", {
        "sr": service_request,
        "comp": computation,
        "address_display": address_display,
        "prepared_by_signature_url": prepared_by_signature_url,
    })


@login_required
def download_computation_pdf(request, pk):
    """Generate and return the computation letter as a PDF download."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    # Only admin, consumer, or requested_by may download. Staff/inspector may not.
    can_view = (
        request.user.is_admin()
        or service_request.consumer == request.user
        or (service_request.requested_by and service_request.requested_by == request.user)
    )
    if not can_view:
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")

    computation = getattr(service_request, "computation", None)
    if not computation:
        messages.warning(request, "Computation not yet available.")
        return redirect("services:request_detail", pk=pk)

    # Only admin may download unfinalized PDF; others only after finalization.
    if not request.user.is_admin() and not computation.is_finalized:
        messages.info(request, "The computation is still being finalized by the administrator.")
        return redirect("services:request_detail", pk=pk)

    try:
        from xhtml2pdf import pisa
    except ImportError:
        messages.error(
            request,
            "PDF download is not available. Install xhtml2pdf: pip install xhtml2pdf",
        )
        return redirect("services:view_computation", pk=pk)

    # Ensure meals personnel count is up to date for the PDF
    completion = getattr(service_request, "completion_info", None)
    if completion:
        desired_personnel = completion.personnel_count
        if computation.personnel_count != desired_personnel:
            computation.personnel_count = desired_personnel
            computation.save()

    addr = (service_request.address or "").strip()
    brgy = (service_request.barangay or "").strip()
    address_display = addr if (brgy and addr.endswith(brgy)) else (f"{addr}, {brgy}" if brgy else addr)

    # Filesystem path for signature so xhtml2pdf can embed the image reliably
    prepared_by_signature_path = None
    if computation.prepared_by_signature and hasattr(computation.prepared_by_signature, "path"):
        prepared_by_signature_path = computation.prepared_by_signature.path

    pdf_font_family = _register_xhtml2pdf_unicode_font()
    pdf_body_font_stack = (
        f"{pdf_font_family}, Helvetica, Arial, sans-serif"
        if pdf_font_family
        else "Helvetica, Arial, sans-serif"
    )

    template = get_template("services/computation_letter_pdf.html")
    html = template.render({
        "sr": service_request,
        "comp": computation,
        "address_display": address_display,
        "prepared_by_signature_path": prepared_by_signature_path,
        "pdf_body_font_stack": pdf_body_font_stack,
    })
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
    """Edit computation (admin/staff only). Recalculates charges on save.
    Admin can also finalize the computation, attach a signature, and send to customer.
    """
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
    form.fields.pop("is_outside_bayawan", None)

    if request.method == "POST":
        action = request.POST.get("action") or "save"
        form = ServiceComputationForm(request.POST, instance=computation)
        form.fields.pop("charge_category", None)
        form.fields.pop("trips", None)
        form.fields.pop("is_outside_bayawan", None)
        if form.is_valid():
            form.save()

            # Update signature whenever a new file is uploaded (for both Save and Finalize)
            sig = request.FILES.get("prepared_by_signature")
            if sig:
                computation.prepared_by_signature = sig

            service_request.fee_amount = computation.total_charge

            if action == "finalize":
                # Attach preparer info, mark finalized, send notifications
                computation.prepared_by = request.user
                computation.is_finalized = True
                computation.finalized_at = timezone.now()
                service_request.status = ServiceRequest.Status.COMPUTATION_SENT
                service_request.save(update_fields=["fee_amount", "status"])

                Notification.objects.create(
                    user=service_request.consumer,
                    message="Your computation letter is ready. You can now view and download it.",
                    notification_type=Notification.NotificationType.COMPUTATION_READY,
                    related_request=service_request,
                )
                if service_request.requested_by and service_request.requested_by != service_request.consumer:
                    Notification.objects.create(
                        user=service_request.requested_by,
                        message="Your computation letter is ready. You can now view and download it.",
                        notification_type=Notification.NotificationType.COMPUTATION_READY,
                        related_request=service_request,
                    )
                messages.success(request, "Computation finalized, signed, and sent to the customer.")
            else:
                # For non-finalized edits, ensure we persist any updated signature or charge changes.
                computation.is_finalized = False
                computation.save(update_fields=["is_finalized"])
                service_request.save(update_fields=["fee_amount"])
                messages.success(request, "Computation updated. Charges recalculated.")

            computation.save()
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
    if not _can_act_on_request(request.user, service_request):
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

            # Update computation payment status to Awaiting Verification (admin will set PAID on confirm)
            computation = getattr(service_request, "computation", None)
            if computation:
                from dashboard.models import ServiceComputation
                if computation.payment_status != ServiceComputation.PaymentStatus.FREE:
                    computation.payment_status = ServiceComputation.PaymentStatus.AWAITING_VERIFICATION
                    computation.save(update_fields=["payment_status"])

            admin_users = User.objects.filter(
                Q(role=User.Role.ADMIN) | Q(is_superuser=True) | Q(is_staff=True)
            )
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
# Inspection fee bill + receipt (first-time customers)
# ---------------------------------------------------------------------------

@login_required
def inspection_fee_bill(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_act_on_request(request.user, service_request):
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")
    if service_request.service_type not in [
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ]:
        messages.error(request, "Inspection fee is only applicable to desludging requests.")
        return redirect("services:request_detail", pk=pk)
    return render(request, "services/inspection_fee_bill.html", {"sr": service_request, "amount": 150})


@login_required
def download_inspection_fee_bill_pdf(request, pk):
    """Generate and return the inspection fee bill as a PDF download."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_act_on_request(request.user, service_request):
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")
    if service_request.service_type not in [
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ]:
        messages.error(request, "Inspection fee is only applicable to desludging requests.")
        return redirect("services:request_detail", pk=pk)

    try:
        from xhtml2pdf import pisa
    except ImportError:
        messages.error(
            request,
            "PDF download is not available. Install xhtml2pdf: pip install xhtml2pdf",
        )
        return redirect("services:inspection_fee_bill", pk=pk)

    template = get_template("services/inspection_fee_bill_pdf.html")
    bayawan_logo_url = request.build_absolute_uri(static("img/bayawan_logo.png"))
    cenro_logo_url = request.build_absolute_uri(static("img/cenro_logo.png"))
    html = template.render(
        {
            "sr": service_request,
            "amount": 150,
            "bayawan_logo_url": bayawan_logo_url,
            "cenro_logo_url": cenro_logo_url,
        }
    )
    result = io.BytesIO()
    pdf = pisa.pisaDocument(
        io.BytesIO(html.encode("utf-8")),
        result,
        encoding="utf-8",
    )
    if pdf.err:
        messages.error(request, "PDF generation failed.")
        return redirect("services:inspection_fee_bill", pk=pk)

    filename = f"inspection-fee-bill-{service_request.id:03d}.pdf"
    response = HttpResponse(result.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def upload_inspection_fee_receipt(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_act_on_request(request.user, service_request):
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")

    if request.method == "POST":
        receipt = request.FILES.get("inspection_fee_receipt")
        if receipt:
            service_request.inspection_fee_receipt = receipt
            service_request.status = ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION
            service_request.save(update_fields=["inspection_fee_receipt", "status"])

            admin_users = User.objects.filter(
                Q(role=User.Role.ADMIN) | Q(is_superuser=True) | Q(is_staff=True)
            )
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=(
                        f"Inspection fee receipt uploaded by {service_request.client_name} "
                        f"for request #{service_request.id}."
                    ),
                    notification_type=Notification.NotificationType.STATUS_CHANGE,
                    related_request=service_request,
                )
            messages.success(request, "Inspection fee receipt uploaded. Waiting for admin verification.")
        else:
            messages.error(request, "Please select a file to upload.")
        return redirect("services:request_detail", pk=pk)

    return render(request, "services/upload_inspection_fee.html", {"sr": service_request})


@login_required
def view_inspection_fee_receipt(request, pk):
    """Serve the uploaded inspection fee receipt so admins can view it reliably."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_view_request_uploaded_files(request.user, service_request):
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")
    if not service_request.inspection_fee_receipt:
        messages.error(request, "No inspection fee receipt uploaded for this request.")
        return redirect("services:request_detail", pk=pk)

    try:
        receipt_file = service_request.inspection_fee_receipt.open("rb")
    except FileNotFoundError:
        messages.error(request, "The uploaded inspection fee receipt file could not be found on the server.")
        return redirect("services:request_detail", pk=pk)

    return FileResponse(receipt_file, as_attachment=False)


def _can_view_request_uploaded_files(user, service_request):
    """Who may open uploaded receipt files (served outside /media/)."""
    if _can_act_on_request(user, service_request):
        return True
    if user.is_staff_member() and service_request.assigned_inspector_id == user.id:
        return True
    return False


@login_required
def view_treasurer_receipt(request, pk):
    """Serve the treasurer payment receipt reliably (avoids broken /media/ URLs in dev)."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_view_request_uploaded_files(request.user, service_request):
        messages.error(request, "Permission denied.")
        return redirect("services:request_list")
    if not service_request.treasurer_receipt:
        messages.error(request, "No payment receipt uploaded for this request.")
        return redirect("services:request_detail", pk=pk)

    try:
        receipt_file = service_request.treasurer_receipt.open("rb")
    except FileNotFoundError:
        messages.error(request, "The uploaded payment receipt file could not be found on the server.")
        return redirect("services:request_detail", pk=pk)

    return FileResponse(receipt_file, as_attachment=False)


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
