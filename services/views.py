from datetime import datetime, timedelta
from decimal import Decimal
import base64

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.forms import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
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
import mimetypes
import time
import os
from uuid import uuid4
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

from accounts.decorators import json_consumer_required, role_required
from accounts.models import User
from scheduling.models import Schedule
from django.templatetags.static import static

from .forms import (
    GRASSCUTTING_RATE_PER_HOUR,
    LOCATION_PHOTO_ALLOWED_EXTENSIONS,
    GrasscuttingAdminEditForm,
    GrasscuttingApplicationForm,
    ServiceRequestForm,
    ServiceRequestStep1Form,
    ServiceRequestStep2Form,
    ServiceRequestStep3Form,
    validate_customer_receipt,
    validate_location_photo,
)
from .location import detect_barangay_for_point, within_service_bounds
from .geocode import (
    address_in_service_area,
    address_names_forbidden_municipality,
    extract_barangay,
    reverse_geocode_osm,
)
from .models import (
    CompletionInfo,
    InspectionDetail,
    Notification,
    ServiceEquipment,
    ServiceRequest,
    ServiceRequestChangeLog,
)

_LOCATION_PHOTO_EXT_LOWER = frozenset(ext.lower() for ext in LOCATION_PHOTO_ALLOWED_EXTENSIONS)

# Session flag: verified "other person" consumer for service request wizard (step 2).
OTHER_VERIFIED_SESSION_KEY = "service_request_other_verified"

# Admin/staff: last Requests list query (tab, filters) for "Back" from request detail.
DASHBOARD_REQUESTS_LIST_SESSION_KEY = "dashboard_requests_list_query"

_DASHBOARD_REQUESTS_TABS = frozenset({"pending", "inspection", "computation", "schedule", "completed"})
_DASHBOARD_REQUESTS_SORT = frozenset({"id", "barangay", "date"})
_DASHBOARD_REQUESTS_DIR = frozenset({"asc", "desc"})
_DASHBOARD_REQUESTS_TYPES = frozenset({"all", "grass", "declogging"})

# Customer self-cancel: not allowed after payment or desludging scheduling (contact CENRO instead).
_CONSUMER_CANCEL_BLOCKED_STATUSES = frozenset(
    {
        ServiceRequest.Status.PAID,
        ServiceRequest.Status.DESLUDGING_SCHEDULED,
        ServiceRequest.Status.COMPLETED,
        ServiceRequest.Status.CANCELLED,
        ServiceRequest.Status.EXPIRED,
    }
)


def _persist_dashboard_requests_list_params(request) -> None:
    """Store tab/sort/filter when opening a request from the dashboard Requests list."""
    if request.GET.get("from") != "dashboard_requests":
        return
    q: dict[str, str] = {}
    tab = request.GET.get("list_tab")
    if tab in _DASHBOARD_REQUESTS_TABS:
        q["tab"] = tab
    rt = request.GET.get("list_request_type")
    if rt in _DASHBOARD_REQUESTS_TYPES and rt != "all":
        q["request_type"] = rt
    s = request.GET.get("list_sort")
    if s in _DASHBOARD_REQUESTS_SORT:
        q["sort"] = s
    d = request.GET.get("list_dir")
    if d in _DASHBOARD_REQUESTS_DIR:
        q["dir"] = d
    request.session[DASHBOARD_REQUESTS_LIST_SESSION_KEY] = q


def _dashboard_requests_list_back_url(request) -> str:
    """Return URL to dashboard Requests with last saved tab/filters (admin/staff)."""
    base = reverse("dashboard:admin_requests")
    q = request.session.get(DASHBOARD_REQUESTS_LIST_SESSION_KEY) or {}
    if not q:
        return base
    return f"{base}?{urlencode(q)}"


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
        street = prof.street_address or ""
        full_line = prof.full_address or ""
        if _addresses_compatible(addr, street) or (
            full_line and _addresses_compatible(addr, full_line)
        ):
            matches.append(u)

    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "multiple"
    return None, "none"


def _consumer_other_verification_valid(request, *field_triples) -> bool:
    """
    Accept session verification if any (client_name, barangay, address) triple matches the
    stored fingerprint or resolves to the same consumer_pk.

    PIN mode overwrites barangay in cleaned_data (detected from map); verify uses raw POST,
    so callers should pass both cleaned and raw triples when available.
    """
    v = request.session.get(OTHER_VERIFIED_SESSION_KEY)
    if not v or not v.get("consumer_pk"):
        return False
    consumer_pk = v["consumer_pk"]
    if not User.objects.filter(
        pk=consumer_pk,
        role=User.Role.CONSUMER,
        is_active=True,
        is_approved=True,
    ).exists():
        request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)
        return False

    seen = set()
    for triple in field_triples:
        if not triple or len(triple) != 3:
            continue
        cn, br, ad = triple
        key = (_norm_key(str(cn or "")), _norm_key(str(br or "")), _norm_key(str(ad or "")))
        if key in seen:
            continue
        if not any(key):
            continue
        seen.add(key)

        fp = _other_verify_fingerprint(cn, br, ad)
        if v.get("fp") == fp:
            return True
        user_obj, _err = find_consumer_by_registered_profile(cn, br, ad)
        if user_obj and user_obj.pk == consumer_pk:
            request.session[OTHER_VERIFIED_SESSION_KEY] = {"consumer_pk": consumer_pk, "fp": fp}
            request.session.modified = True
            return True
    return False


def _other_verification_triples_from_form_data(form_data) -> list[tuple]:
    """Primary row is canonical wizard data; optional _verify_post_* holds raw step-2 POST for 'other' flow."""
    primary = (
        form_data.get("client_name") or "",
        form_data.get("barangay") or "",
        form_data.get("address") or "",
    )
    out: list[tuple] = [primary]
    if form_data.get("request_for") == ServiceRequestStep2Form.REQUEST_FOR_OTHER:
        raw = (
            form_data.get("_verify_post_client_name") or "",
            form_data.get("_verify_post_barangay") or "",
            form_data.get("_verify_post_address") or "",
        )
        if any((x or "").strip() for x in raw):
            out.append(raw)
    return out


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


def _admin_notification_recipients():
    """
    Active accounts that should receive operational alerts (new requests, receipt uploads, etc.).
    Includes portal ADMIN and STAFF roles, plus Django superuser / is_staff for edge cases.
    """
    return (
        User.objects.filter(is_active=True)
        .filter(
            Q(role__in=(User.Role.ADMIN, User.Role.STAFF))
            | Q(is_superuser=True)
            | Q(is_staff=True)
        )
        .distinct()
    )


def _notify_admin_users(message, notification_type, related_request=None):
    """Create one in-app Notification for each admin/staff recipient."""
    users = list(_admin_notification_recipients())
    if not users:
        return
    msg = (message or "")[:500]
    Notification.objects.bulk_create(
        [
            Notification(
                user=u,
                message=msg,
                notification_type=notification_type,
                related_request=related_request,
            )
            for u in users
        ]
    )


def _can_view_grass_application_receipt(user, service_request):
    """Consumers/requesters, admins, and staff may view the grass application form receipt."""
    if service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING:
        return False
    if user.is_admin() or user.is_staff_member():
        return True
    return _can_act_on_request(user, service_request)


def _summarize_form_errors(error_dict) -> str:
    """Short human-readable summary for API / JSON responses."""
    if not error_dict:
        return "Check the form fields and try again."
    for field, errs in error_dict.items():
        if errs:
            label = "Form" if field in (None, "__all__") else str(field).replace("_", " ")
            try:
                first = errs[0]
            except (TypeError, IndexError):
                first = errs
            return f"{label}: {first}"
    return "Check the form fields and try again."


def _message_uploaded_file_access_denied(request, service_request, what_phrase: str) -> None:
    """
    User-facing explanation when a receipt or location photo cannot be opened.
    what_phrase examples: "this payment receipt", "this inspection fee receipt", "this location photo"
    """
    user = request.user
    if user.is_staff_member() and not user.is_admin():
        if service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING:
            if service_request.assigned_inspector_id != user.id:
                messages.error(
                    request,
                    f"As an inspector, you can open {what_phrase} only for requests assigned to you. "
                    f"Request #{service_request.pk} is not on your assignment list. "
                    "Ask an administrator if you should be assigned to this request.",
                )
                return
    if getattr(user, "role", None) == User.Role.CONSUMER:
        messages.error(
            request,
            f"You can open {what_phrase} only for service requests tied to your CENRO Sanitary Management System account "
            "(as the customer or the person who submitted the request). "
            "Use My Service Requests or Service History to find the correct request.",
        )
        return
    messages.error(
        request,
        f"You do not have access to {what_phrase} for request #{service_request.pk}.",
    )


def _message_computation_letter_access_denied(request, service_request) -> None:
    user = request.user
    if user.is_staff_member() and not user.is_admin():
        messages.error(
            request,
            "Cost computation letters are shared with the customer and administrators only. "
            "As field staff, use Service Request details to review inspection, location, and schedules.",
        )
        return
    messages.error(
        request,
        "Only the registered customer (or the person who submitted the request on their behalf) "
        "can open this computation letter. If you opened a link from another account, return to "
        "My Service Requests and select your own request.",
    )


def _message_receipt_upload_denied(request) -> None:
    messages.error(
        request,
        "You can upload payment receipts only for your own service requests — where you are the "
        "registered customer or the person who submitted the request on their behalf.",
    )


def _message_inspection_fee_page_denied(request) -> None:
    messages.error(
        request,
        "Inspection fee documents apply only to your own septage (desludging) requests. "
        "Open the request from My Service Requests or Service History.",
    )


_COMPUTATION_NOT_READY_MSG = (
    "No cost computation has been prepared for this request yet. After inspection (or waived inspection) "
    "and office processing, an administrator will enter charges — then the computation letter will be available here."
)


# ---------------------------------------------------------------------------
# Multi-step service request wizard (3 steps)
# ---------------------------------------------------------------------------

@require_POST
@json_consumer_required
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
    register_url = request.build_absolute_uri(register_path + "?" + urlencode({"from": "sr_other"}))

    messages_map = {
        "missing_name": "Enter the client's full name (as registered in CENRO Sanitary Management System).",
        "missing_barangay": "Set the service barangay (from the map or by typing it) before verifying.",
        "missing_address": (
            "Enter the street address as it appears on the client's profile in CENRO Sanitary Management System "
            "(Complete Address / Landmark), then verify again."
        ),
        "none": (
            "No approved consumer account on CENRO Sanitary Management System matches this name, barangay, and address. "
            "The client must register a consumer account on CENRO Sanitary Management System first so their record is on file; "
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
@require_POST
def offline_create_request(request):
    """
    One-shot request creation endpoint for offline replay of the multi-step wizard.
    Server remains authoritative for validation and conflict behavior.
    """
    service_type = (request.POST.get("service_type") or "").strip()
    if service_type == ServiceRequest.ServiceType.GRASS_CUTTING:
        return JsonResponse(
            {
                "ok": False,
                "message": "Grass Cutting uses a different application flow. Please submit it from the Grass Cutting form.",
            },
            status=400,
        )

    request_for = (request.POST.get("request_for") or ServiceRequestStep2Form.REQUEST_FOR_OWNER).strip()

    form = ServiceRequestStep2Form(
        request.POST,
        request.FILES,
        service_type=service_type or ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
    )
    if not form.is_valid():
        return JsonResponse(
            {
                "ok": False,
                "errors": form.errors,
                "message": _summarize_form_errors(form.errors),
            },
            status=400,
        )

    cd = form.cleaned_data
    target_consumer = request.user
    requested_by_user = None
    if request_for == ServiceRequestStep2Form.REQUEST_FOR_OTHER:
        user_obj, err2 = find_consumer_by_registered_profile(
            cd.get("client_name"),
            cd.get("barangay"),
            cd.get("address"),
        )
        if err2 or not user_obj:
            return JsonResponse(
                {
                    "ok": False,
                    "message": "Offline sync rejected: client account verification no longer matches server records.",
                    "code": err2 or "none",
                },
                status=409,
            )
        target_consumer = user_obj
        requested_by_user = request.user

    try:
        ServiceRequest.expire_stale_requests()
    except Exception:
        pass

    if ServiceRequest.consumer_has_open_request_same_type(
        target_consumer,
        service_type or ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
    ):
        return JsonResponse(
            {"ok": False, "message": "An ongoing request already exists for this owner and service type."},
            status=409,
        )

    service_request = ServiceRequest.objects.create(
        consumer=target_consumer,
        requested_by=requested_by_user,
        client_name=cd.get("client_name") or request.user.get_full_name(),
        request_date=cd.get("request_date") or timezone.now().date(),
        contact_number=cd.get("contact_number") or "",
        location_mode=cd.get("location_mode") or ServiceRequest.LocationMode.PIN,
        barangay=cd.get("barangay") or "",
        address=cd.get("address") or "",
        gps_latitude=cd.get("gps_latitude"),
        gps_longitude=cd.get("gps_longitude"),
        service_type=service_type or ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        connected_to_bawad=(cd.get("connected_to_bawad") == "YES"),
        public_private=cd.get("public_private") or ServiceRequest.PublicPrivate.PRIVATE,
        status=ServiceRequest.Status.SUBMITTED,
    )

    if cd.get("bawad_proof"):
        service_request.bawad_proof = cd["bawad_proof"]
    if cd.get("client_signature"):
        service_request.client_signature = cd["client_signature"]
    elif cd.get("client_signature_data"):
        try:
            raw = str(cd.get("client_signature_data") or "")
            if "," in raw:
                _, raw = raw.split(",", 1)
            sig_bytes = base64.b64decode(raw)
            service_request.client_signature.save(
                f"offline_signature_{uuid4().hex}.png",
                ContentFile(sig_bytes),
                save=False,
            )
        except Exception:
            pass
    if cd.get("location_photo_1"):
        service_request.location_photo_1 = cd["location_photo_1"]
    if cd.get("location_photo_2"):
        service_request.location_photo_2 = cd["location_photo_2"]
    service_request.save()

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
            legacy_vol = getattr(
                getattr(target_consumer, "consumer_profile", None),
                "prior_desludging_m3_4y", 0,
            ) or 0
            if legacy_vol <= 0:
                if service_request.qualifies_public_bayawan_no_fees:
                    service_request.apply_public_bayawan_inspection_fee_waiver(
                        notify_user=service_request.consumer,
                    )
                else:
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

    _notify_admin_users(
        f"New {service_request.get_service_type_display()} request from {service_request.client_name}.",
        Notification.NotificationType.REQUEST_SUBMITTED,
        service_request,
    )
    Notification.objects.create(
        user=request.user,
        message=f"Your {service_request.get_service_type_display()} request has been submitted.",
        notification_type=Notification.NotificationType.STATUS_CHANGE,
        related_request=service_request,
    )

    return JsonResponse(
        {
            "ok": True,
            "request_id": service_request.id,
            "status": service_request.status,
            "message": "Offline request synced successfully.",
        }
    )


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

    # Footer / deep links: pre-select service type on step 1 (GET only)
    if request.method == "GET" and step == 1:
        prefill = (request.GET.get("prefill") or "").strip().lower()
        prefill_map = {
            "grass": ServiceRequest.ServiceType.GRASS_CUTTING,
            "grasscutting": ServiceRequest.ServiceType.GRASS_CUTTING,
            "grass_cutting": ServiceRequest.ServiceType.GRASS_CUTTING,
            "septage": ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
            "desludging": ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
            "residential": ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
            "commercial": ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
        }
        if prefill in prefill_map:
            form_data["service_type"] = prefill_map[prefill]
            request.session["service_request_data"] = form_data

    form = None

    if request.method == "POST":
        # ---- Step 1: Service Type ----
        if step == 1:
            form = ServiceRequestStep1Form(request.POST)
            if form.is_valid():
                new_type = form.cleaned_data["service_type"]
                if new_type == ServiceRequest.ServiceType.GRASS_CUTTING:
                    old_bt = form_data.pop("_bawad_proof_temp", None)
                    if old_bt:
                        try:
                            default_storage.delete(old_bt)
                        except Exception:
                            pass
                    request.session.pop("_bawad_proof_pending", None)
                form_data["service_type"] = new_type
                request.session["service_request_data"] = form_data
                return HttpResponseRedirect(reverse("services:create_request") + "?step=2")

        # ---- Step 2: Customer Request Form ----
        elif step == 2:
            form = ServiceRequestStep2Form(
                request.POST,
                request.FILES,
                service_type=form_data.get("service_type"),
                existing_bawad_proof_temp=form_data.get("_bawad_proof_temp"),
            )
            if form.is_valid():
                request_for_val = (
                    form.cleaned_data.get("request_for") or ServiceRequestStep2Form.REQUEST_FOR_OWNER
                )
                if request_for_val == ServiceRequestStep2Form.REQUEST_FOR_OWNER:
                    request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)
                elif not _consumer_other_verification_valid(
                    request,
                    (
                        form.cleaned_data.get("client_name"),
                        form.cleaned_data.get("barangay"),
                        form.cleaned_data.get("address"),
                    ),
                    (
                        form.data.get("client_name", ""),
                        form.data.get("barangay", ""),
                        form.data.get("address", ""),
                    ),
                ):
                    messages.error(
                        request,
                        'Please click "Verify client account" and confirm the client\'s name, barangay, and street '
                        "address match their registered profile in CENRO Sanitary Management System before continuing.",
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
                            "step2_other_verified": _consumer_other_verification_valid(
                                request,
                                (
                                    form.cleaned_data.get("client_name"),
                                    form.cleaned_data.get("barangay"),
                                    form.cleaned_data.get("address"),
                                ),
                                (
                                    form.data.get("client_name", ""),
                                    form.data.get("barangay", ""),
                                    form.data.get("address", ""),
                                ),
                            ),
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
                _req_for = form.cleaned_data.get("request_for") or ServiceRequestStep2Form.REQUEST_FOR_OWNER
                if _req_for == ServiceRequestStep2Form.REQUEST_FOR_OTHER:
                    form_data["_verify_post_client_name"] = (request.POST.get("client_name") or "").strip()
                    form_data["_verify_post_barangay"] = (request.POST.get("barangay") or "").strip()
                    form_data["_verify_post_address"] = (request.POST.get("address") or "").strip()
                else:
                    form_data.pop("_verify_post_client_name", None)
                    form_data.pop("_verify_post_barangay", None)
                    form_data.pop("_verify_post_address", None)

                # BAWAD proof: stage to temp storage (finalized onto ServiceRequest in step 3).
                bawad_yes = (form.cleaned_data.get("connected_to_bawad") or "NO") == "YES"
                if not bawad_yes:
                    old_bt = form_data.pop("_bawad_proof_temp", None)
                    if old_bt:
                        try:
                            default_storage.delete(old_bt)
                        except Exception:
                            pass
                    request.session.pop("_bawad_proof_pending", None)
                else:
                    bawad_file = request.FILES.get("bawad_proof")
                    if bawad_file:
                        ext = (os.path.splitext(bawad_file.name)[1] or ".pdf").lower()
                        if ext not in (".pdf", ".jpg", ".jpeg", ".png", ".webp"):
                            ext = ".pdf"
                        path = f"bawad_proofs/temp/{uuid4()}{ext}"
                        default_storage.save(path, ContentFile(bawad_file.read()))
                        old_bt = form_data.get("_bawad_proof_temp")
                        if old_bt and old_bt != path:
                            try:
                                default_storage.delete(old_bt)
                            except Exception:
                                pass
                        form_data["_bawad_proof_temp"] = path
                        request.session["_bawad_proof_pending"] = True
                    elif form_data.get("_bawad_proof_temp"):
                        request.session["_bawad_proof_pending"] = True
                    else:
                        request.session.pop("_bawad_proof_pending", None)

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
                            ext = (os.path.splitext(f.name)[1] or ".jpg").lower()
                            if ext not in _LOCATION_PHOTO_EXT_LOWER:
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
                    if not _consumer_other_verification_valid(
                        request,
                        *_other_verification_triples_from_form_data(form_data),
                    ):
                        messages.error(
                            request,
                            "Verification is missing or out of date. Please return to step 2 and verify the "
                            "client's registered account again.",
                        )
                        return redirect(reverse("services:create_request") + "?step=2")
                    v = request.session.get(OTHER_VERIFIED_SESSION_KEY)
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
                try:
                    ServiceRequest.expire_stale_requests()
                except Exception:
                    pass
                if ServiceRequest.consumer_has_open_request_same_type(
                    target_consumer,
                    form_data.get("service_type", "RESIDENTIAL_DESLUDGING"),
                ):
                    messages.error(
                        request,
                        "You already have an ongoing request of this service type for this owner. "
                        "Please complete or cancel the existing request before submitting a new one.",
                    )
                    return redirect("services:history")

                st_submit = form_data.get("service_type", "RESIDENTIAL_DESLUDGING")
                is_declog_submit = st_submit in (
                    ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
                    ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
                )
                if is_declog_submit and form_data.get("connected_to_bawad") == "YES":
                    bt = form_data.get("_bawad_proof_temp")
                    if not bt or not default_storage.exists(bt):
                        messages.error(
                            request,
                            "BAWAD affiliation proof is missing or expired. Please return to step 2 and upload proof again.",
                        )
                        return redirect(reverse("services:create_request") + "?step=2")

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
                bawad_temp = form_data.get("_bawad_proof_temp")
                if bawad_temp and default_storage.exists(bawad_temp):
                    try:
                        with default_storage.open(bawad_temp, "rb") as fp:
                            name = os.path.basename(bawad_temp)
                            service_request.bawad_proof.save(name, ContentFile(fp.read()), save=True)
                    except Exception:
                        pass
                    try:
                        default_storage.delete(bawad_temp)
                    except Exception:
                        pass

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
                        legacy_vol = getattr(
                            getattr(target_consumer, "consumer_profile", None),
                            "prior_desludging_m3_4y", 0,
                        ) or 0
                        if legacy_vol <= 0:
                            if service_request.qualifies_public_bayawan_no_fees:
                                service_request.apply_public_bayawan_inspection_fee_waiver(
                                    notify_user=service_request.consumer,
                                )
                            else:
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

                _notify_admin_users(
                    f"New {service_request.get_service_type_display()} request from {service_request.client_name}.",
                    Notification.NotificationType.REQUEST_SUBMITTED,
                    service_request,
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
                existing_bawad_proof_temp=form_data.get("_bawad_proof_temp"),
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
        triples = list(_other_verification_triples_from_form_data(form_data))
        if form is not None and getattr(form, "is_bound", False):
            d = form.data
            triples.append(
                (d.get("client_name", ""), d.get("barangay", ""), d.get("address", ""))
            )
        step2_other_verified = _consumer_other_verification_valid(request, *triples)

    context = {
        "form": form,
        "step": step,
        "form_data": form_data,
        "owner_profile_json": json.dumps(owner_profile),
        "verify_other_consumer_url": verify_other_consumer_url,
        "consumer_register_url": consumer_register_url,
        "step2_other_verified": step2_other_verified,
        "step2_bawad_proof_saved": bool(form_data.get("_bawad_proof_temp")) if step == 2 else False,
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
            *_other_verification_triples_from_form_data(form_data),
        ):
            messages.error(
                request,
                "Client verification is missing or out of date. Please complete step 2 and verify the "
                "registered CENRO Sanitary Management System account before continuing.",
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
        gc_sig_file = request.FILES.get("gc_client_signature")
        gc_sig_data = (request.POST.get("gc_signature_data") or "").strip()
        sig_photo_error = None
        if gc_sig_file and not gc_sig_data.startswith("data:image"):
            try:
                validate_location_photo(gc_sig_file)
            except ValidationError as e:
                sig_photo_error = next(iter(e.messages))

        form = GrasscuttingApplicationForm(request.POST, initial=initial)
        if not form.is_valid():
            pass
        elif sig_photo_error:
            messages.error(request, sig_photo_error)
        else:
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
                f"Place of Grass Cutting: {cd.get('place_of_grass_cutting') or form_data.get('barangay') or ''}",
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
                if not _consumer_other_verification_valid(
                    request,
                    *_other_verification_triples_from_form_data(form_data),
                ):
                    messages.error(
                        request,
                        "Verification is missing or out of date. Please return to step 2 and verify again.",
                    )
                    return redirect(reverse("services:create_request") + "?step=2")
                v = request.session.get(OTHER_VERIFIED_SESSION_KEY)
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

            try:
                ServiceRequest.expire_stale_requests()
            except Exception:
                pass

            if ServiceRequest.consumer_has_open_request_same_type(
                target_consumer,
                ServiceRequest.ServiceType.GRASS_CUTTING,
            ):
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
                status=ServiceRequest.Status.GRASS_PENDING_PAYMENT,
                notes=notes,
                fee_amount=Decimal(str(round(total_amount, 2))),
                grasscutting_date=cd["date_of_grass_cutting"],
                grasscutting_personnel=personnel,
                grasscutting_hours=Decimal(str(hours)),
            )

            # Save client signature (upload or drawn canvas data URL)
            gc_sig_file = request.FILES.get("gc_client_signature")
            gc_sig_data = (request.POST.get("gc_signature_data") or "").strip()
            try:
                if gc_sig_data and gc_sig_data.startswith("data:image"):
                    header, b64_data = gc_sig_data.split(",", 1)
                    ext = ".png"
                    if "jpeg" in header or "jpg" in header:
                        ext = ".jpg"
                    elif "webp" in header:
                        ext = ".webp"
                    binary = base64.b64decode(b64_data)
                    # Basename only — upload_to already prefixes client_signatures/
                    service_request.client_signature.save(
                        f"gc_{service_request.id}{ext}",
                        ContentFile(binary),
                        save=True,
                    )
                elif gc_sig_file:
                    ext = (os.path.splitext(gc_sig_file.name)[1] or ".jpg").lower()
                    if ext not in _LOCATION_PHOTO_EXT_LOWER:
                        ext = ".jpg"
                    service_request.client_signature.save(
                        f"gc_{service_request.id}{ext}",
                        ContentFile(gc_sig_file.read()),
                        save=True,
                    )
            except Exception:
                pass

            _notify_admin_users(
                f"New Grass Cutting request from {service_request.client_name} (pending payment at Treasurer).",
                Notification.NotificationType.REQUEST_SUBMITTED,
                service_request,
            )
            Notification.objects.create(
                user=request.user,
                message="Your Grass Cutting application was received. Pay at the Treasurer's Office, then upload your payment receipt.",
                notification_type=Notification.NotificationType.STATUS_CHANGE,
                related_request=service_request,
            )

            request.session.pop("service_request_data", None)
            request.session.pop("_bawad_proof_pending", None)
            request.session.pop("_client_sig_pending", None)
            request.session.pop("_location_photos", None)
            request.session.pop(OTHER_VERIFIED_SESSION_KEY, None)

            messages.success(
                request,
                "Application received. Review your official receipt, pay at the Treasurer's Office, then upload your payment proof.",
            )
            return redirect("services:grasscutting_application_receipt", pk=service_request.pk)
    else:
        form = GrasscuttingApplicationForm(initial=initial)

    return render(request, "services/grasscutting_application.html", {
        "form": form,
        "form_data": form_data,
        "rate_per_hour": GRASSCUTTING_RATE_PER_HOUR,
    })


@login_required
def grasscutting_application_receipt(request, pk):
    """Official application-form receipt (reference copy for consumer and admin)."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_view_grass_application_receipt(request.user, service_request):
        if service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING:
            messages.error(
                request,
                "This printable application receipt exists only for Grass Cutting requests.",
            )
        else:
            messages.error(
                request,
                "You can open this receipt only as the customer, the person who submitted the request, "
                "or as CENRO staff reviewing the case. Use My Service Requests to find your Grass Cutting request.",
            )
        return redirect("services:request_list")

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

    notes = service_request.notes or ""
    designated_time = _extract_grasscutting_field(notes, "Designated Time:")
    place_of_grass_cutting = _extract_grasscutting_field(notes, "Place of Grass Cutting:")
    signature_name = _extract_grasscutting_field(notes, "Signature over printed name:")
    reference_number = f"ECO-{timezone.now().year}-{service_request.id % 1000:03d}"
    total_amount = service_request.fee_amount
    if total_amount is None and g_personnel and g_hours:
        total_amount = Decimal(
            str(round(float(g_personnel) * float(g_hours) * float(GRASSCUTTING_RATE_PER_HOUR), 2))
        )

    return render(
        request,
        "services/grasscutting_application_receipt.html",
        {
            "sr": service_request,
            "reference_number": reference_number,
            "grasscutting_date": g_date,
            "grasscutting_personnel": g_personnel,
            "grasscutting_hours": g_hours,
            "designated_time": designated_time,
            "place_of_grass_cutting": place_of_grass_cutting,
            "signature_name": signature_name or service_request.client_name,
            "rate_per_hour": GRASSCUTTING_RATE_PER_HOUR,
            "total_amount": total_amount,
        },
    )


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

    admin_edit_blocked = service_request.status in (
        ServiceRequest.Status.COMPLETED,
        ServiceRequest.Status.CANCELLED,
    ) or (
        service_request.status == ServiceRequest.Status.GRASS_PAYMENT_AWAITING_VERIFICATION
        and service_request.treasurer_receipt
    )

    if admin_edit_blocked and request.method == "POST":
        messages.error(
            request,
            "This application can no longer be edited at its current status.",
        )
        return redirect("services:grasscutting_request_detail", pk=pk)

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
        "admin_edit_blocked": admin_edit_blocked,
    })


@login_required
def grasscutting_request_view(request, pk):
    """Consumer view: read-only Grasscutting Application Form + admin changes."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING:
        messages.error(request, "This page is only for Grass Cutting requests.")
        return redirect("services:request_detail", pk=pk)

    if not (
        _can_act_on_request(request.user, service_request)
        or request.user.is_admin()
        or request.user.is_staff_member()
    ):
        messages.error(
            request,
            "You can view this Grass Cutting application only when you are the customer, submitted it on someone's behalf, "
            "or are logged in as CENRO staff. Open the request from My Service Requests or the admin request list.",
        )
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
        return JsonResponse(
            {"ok": False, "error": "Map lookup needs latitude and longitude. Tap the map again or refresh the page."},
            status=400,
        )

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except ValueError:
        return JsonResponse(
            {"ok": False, "error": "The map sent coordinates this app could not read. Pin the location again on the map."},
            status=400,
        )

    detected = detect_barangay_for_point(lat_f, lon_f)

    cache_key = f"{round(lat_f, 6)},{round(lon_f, 6)}"
    cached = _rg_cache_get(cache_key)
    if cached:
        display_name = cached.get("display_name")
        address = cached.get("address") or {}
        within_bayawan = within_service_bounds(lat_f, lon_f) or address_in_service_area(
            address, display_name
        )
        if within_bayawan and address_names_forbidden_municipality(address, display_name):
            within_bayawan = False

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
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "Address lookup from the map is temporarily unavailable (map service did not respond). "
                    "Try again in a moment, or switch to 'Type Address' and enter barangay and street manually."
                ),
            },
            status=502,
        )

    address = data.get("address") or {}
    display_name = data.get("display_name")

    within_bayawan = within_service_bounds(lat_f, lon_f) or address_in_service_area(
        address, display_name
    )
    if within_bayawan and address_names_forbidden_municipality(address, display_name):
        within_bayawan = False
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
    try:
        ServiceRequest.expire_stale_requests()
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
                    ServiceRequest.Status.GRASS_PENDING_PAYMENT,
                    ServiceRequest.Status.GRASS_PAYMENT_AWAITING_VERIFICATION,
                ],
            )
        ).order_by("-created_at")
    return render(request, "services/request_list.html", {"requests": requests_qs})


@login_required
def request_detail(request, pk):
    try:
        ServiceRequest.expire_stale_requests()
    except Exception:
        pass

    service_request = get_object_or_404(ServiceRequest, pk=pk)
    is_admin = request.user.is_admin()
    is_staff = request.user.is_staff_member()
    is_admin_like = is_admin or is_staff

    # Consumers: can view their own requests plus any request they submitted
    # on behalf of another person (requested_by), at all statuses.
    if not is_admin_like and not _can_act_on_request(request.user, service_request):
        messages.error(
            request,
            "You can open this request only if you are the registered customer or the person who submitted it "
            "on their behalf. CENRO staff should open requests from the dashboard or assigned list.",
        )
        return redirect("services:request_list")

    # Staff: can only view requests assigned to them (grass cutting is not inspector-assigned).
    if is_staff:
        if service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING:
            if service_request.assigned_inspector_id != request.user.id:
                messages.error(
                    request,
                    f"As an inspector, you can only open request #{service_request.pk} if it is assigned to you. "
                    "Check Pending assignments or ask an administrator to assign this inspection.",
                )
                return redirect("services:request_list")

    _persist_dashboard_requests_list_params(request)

    # When an admin/staff opens a newly submitted request, automatically move it to "Under Review".
    # Grass Cutting uses its own payment-first statuses; do not auto-advance those rows.
    if (
        is_admin_like
        and service_request.status == ServiceRequest.Status.SUBMITTED
        and service_request.service_type != ServiceRequest.ServiceType.GRASS_CUTTING
    ):
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

        inspection_waived = "[NO_INSPECTION_FEE]" in (service_request.notes or "")

        can_assign_inspector = (
            service_request.status
            in [SRModel.Status.UNDER_REVIEW, SRModel.Status.SUBMITTED]
            and not has_prior_inspected
            and not inspection_waived
        )
        inspection_optional = (
            service_request.status
            in [SRModel.Status.UNDER_REVIEW, SRModel.Status.SUBMITTED]
            and has_prior_inspected
            and not inspection_waived
        )

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

    _comp = getattr(service_request, "computation", None)
    can_finalize_letter = bool(
        is_admin
        and _comp
        and not _comp.is_finalized
        and _comp.ready_to_finalize
    )

    if is_admin_like:
        requests_list_back_url = _dashboard_requests_list_back_url(request)
    else:
        requests_list_back_url = reverse("services:request_list")

    bawad_proof_is_image = False
    if service_request.bawad_proof:
        _ct, _ = mimetypes.guess_type(service_request.bawad_proof.name or "")
        bawad_proof_is_image = bool(_ct and _ct.startswith("image/"))

    can_waive_bawad_inspection_fee = (
        request.user.is_admin()
        and service_request.connected_to_bawad
        and bool(service_request.bawad_proof)
        and not service_request.qualifies_public_bayawan_no_fees
        and service_request.service_type
        in (
            ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
            ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
        )
        and service_request.status
        in (
            ServiceRequest.Status.INSPECTION_FEE_DUE,
            ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION,
        )
    )

    can_admin_reject_request = request.user.is_admin() and service_request.status not in (
        ServiceRequest.Status.CANCELLED,
        ServiceRequest.Status.COMPLETED,
        ServiceRequest.Status.EXPIRED,
    )

    can_consumer_cancel_request = (
        not request.user.is_admin()
        and not request.user.is_staff_member()
        and (
            service_request.consumer_id == request.user.id
            or (
                service_request.requested_by_id
                and service_request.requested_by_id == request.user.id
            )
        )
        and service_request.status not in _CONSUMER_CANCEL_BLOCKED_STATUSES
    )

    context = {
        "sr": service_request,
        "staff_members": staff_members,
        "can_assign_inspector": can_assign_inspector,
        "inspection_optional": inspection_optional,
        "inspection_waived": inspection_waived,
        "waived_crew_ready": service_request.waived_inspection_crew_ready,
        "has_prior_inspected": has_prior_inspected,
        "inspector_label": inspector_label,
        "inspection_time": inspection_time,
        "inspection_reason": inspection_reason,
        "desludging_time": desludging_time,
        "can_finalize_letter": can_finalize_letter,
        "requests_list_back_url": requests_list_back_url,
        "bawad_proof_is_image": bawad_proof_is_image,
        "can_waive_bawad_inspection_fee": can_waive_bawad_inspection_fee,
        "can_admin_reject_request": can_admin_reject_request,
        "can_consumer_cancel_request": can_consumer_cancel_request,
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
            messages.error(
                request,
                f"Inspectors can submit the inspection form only while the request is in 'Inspection Scheduled'. "
                f"Current status: {service_request.get_status_display()}. "
                "If you already submitted inspection data, ask an administrator to make changes.",
            )
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
            "equipment": None,
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
    """Admin fills completion info, triggers computation generation."""
    from .models import DesludgingPersonnel
    from .personnel_schedule import find_personnel_schedule_conflicts, get_desludging_timeslot_for_request

    service_request = get_object_or_404(ServiceRequest, pk=pk)
    # OneToOne reverse access raises if missing; template must not use sr.inspection_detail directly.
    insp_detail = InspectionDetail.objects.filter(service_request_id=service_request.pk).first()
    personnel_drivers = DesludgingPersonnel.objects.filter(
        role=DesludgingPersonnel.Role.DRIVER, is_active=True
    ).order_by("full_name")
    personnel_helpers = DesludgingPersonnel.objects.filter(
        role=DesludgingPersonnel.Role.HELPER, is_active=True
    ).order_by("full_name")

    def _equipment_choices_for_form(extra_equipment=None):
        active_qs = ServiceEquipment.objects.filter(is_active=True).order_by("unit_number")
        rows = list(active_qs)
        existing_ci = getattr(service_request, "completion_info", None)
        if existing_ci and existing_ci.equipment_id and not existing_ci.equipment.is_active:
            if existing_ci.equipment not in rows:
                rows.insert(0, existing_ci.equipment)
        if extra_equipment and extra_equipment not in rows:
            rows.insert(0, extra_equipment)
        return rows

    if request.method == "POST":
        service_equipment = _equipment_choices_for_form()
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

        driver_name = (request.POST.get("driver_name") or "").strip()
        helper1_name = (request.POST.get("helper1_name") or "").strip()
        helper2_name = (request.POST.get("helper2_name") or "").strip()
        helper3_name = (request.POST.get("helper3_name") or "").strip()

        def _completion_personnel_key(name):
            return " ".join((name or "").split()).casefold()

        driver_key = _completion_personnel_key(driver_name)
        _helper_seen = set()
        for slot_label, h_raw in (
            ("Helper 1", helper1_name),
            ("Helper 2", helper2_name),
            ("Helper 3", helper3_name),
        ):
            hk = _completion_personnel_key(h_raw)
            if not hk:
                continue
            if driver_key and hk == driver_key:
                messages.error(
                    request,
                    f"{slot_label} cannot be the same person as the driver. Choose a different helper or change the driver.",
                )
                return render(
                    request,
                    "services/completion_form.html",
                    {
                        "sr": service_request,
                        "inspection_detail": insp_detail,
                        "personnel_drivers": personnel_drivers,
                        "personnel_helpers": personnel_helpers,
                        "service_equipment": _equipment_choices_for_form(),
                        "posted": {
                            "equipment_id": (request.POST.get("equipment_id") or "").strip(),
                            "driver_name": driver_name,
                            "helper1_name": helper1_name,
                            "helper2_name": helper2_name,
                            "helper3_name": helper3_name,
                        },
                    },
                )
            if hk in _helper_seen:
                messages.error(
                    request,
                    "The same person cannot be selected for more than one helper slot. Each helper must be unique.",
                )
                return render(
                    request,
                    "services/completion_form.html",
                    {
                        "sr": service_request,
                        "inspection_detail": insp_detail,
                        "personnel_drivers": personnel_drivers,
                        "personnel_helpers": personnel_helpers,
                        "service_equipment": _equipment_choices_for_form(),
                        "posted": {
                            "equipment_id": (request.POST.get("equipment_id") or "").strip(),
                            "driver_name": driver_name,
                            "helper1_name": helper1_name,
                            "helper2_name": helper2_name,
                            "helper3_name": helper3_name,
                        },
                    },
                )
            _helper_seen.add(hk)

        raw_eq_id = (request.POST.get("equipment_id") or "").strip()
        equipment = None
        declogger_no = ""
        equipment_id_str = ""
        if raw_eq_id:
            try:
                eq = ServiceEquipment.objects.get(pk=int(raw_eq_id))
            except (ValueError, TypeError, ServiceEquipment.DoesNotExist):
                messages.error(
                    request,
                    "The equipment unit you chose is not in the active list (it may have been removed or the page is outdated). "
                    "Refresh the page and pick a unit from the dropdown again.",
                )
                return render(
                    request,
                    "services/completion_form.html",
                    {
                        "sr": service_request,
                        "inspection_detail": insp_detail,
                        "personnel_drivers": personnel_drivers,
                        "personnel_helpers": personnel_helpers,
                        "service_equipment": service_equipment,
                        "posted": {
                            "equipment_id": raw_eq_id,
                            "driver_name": driver_name,
                            "helper1_name": helper1_name,
                            "helper2_name": helper2_name,
                            "helper3_name": helper3_name,
                        },
                    },
                )
            if not eq.is_active:
                allowed = (
                    existing_completion
                    and existing_completion.equipment_id == eq.pk
                )
                if not allowed:
                    messages.error(
                        request,
                        "That equipment is inactive. Choose another unit or reactivate it under Equipment.",
                    )
                    return render(
                        request,
                        "services/completion_form.html",
                        {
                            "sr": service_request,
                            "inspection_detail": insp_detail,
                            "personnel_drivers": personnel_drivers,
                            "personnel_helpers": personnel_helpers,
                            "service_equipment": _equipment_choices_for_form(
                                extra_equipment=eq
                            ),
                            "posted": {
                                "equipment_id": str(eq.pk),
                                "driver_name": driver_name,
                                "helper1_name": helper1_name,
                                "helper2_name": helper2_name,
                                "helper3_name": helper3_name,
                            },
                        },
                    )
            equipment = eq
            declogger_no = eq.unit_number
            equipment_id_str = str(eq.pk)

        sched_date, sched_t_norm = get_desludging_timeslot_for_request(service_request)
        overlap_conflicts = find_personnel_schedule_conflicts(
            exclude_request_id=service_request.pk,
            sched_date=sched_date,
            sched_time_normalized=sched_t_norm,
            selected_names=[driver_name, helper1_name, helper2_name, helper3_name],
        )
        override_overlap = request.POST.get("override_personnel_schedule_overlap") == "1"
        if overlap_conflicts and not override_overlap:
            messages.warning(
                request,
                "Selected driver or helper may already be assigned to another job at this date and time. "
                "Review the warning and check the box to confirm before saving.",
            )
            return render(
                request,
                "services/completion_form.html",
                {
                    "sr": service_request,
                    "inspection_detail": insp_detail,
                    "personnel_drivers": personnel_drivers,
                    "personnel_helpers": personnel_helpers,
                    "service_equipment": service_equipment,
                    "personnel_schedule_conflicts": overlap_conflicts,
                    "posted": {
                        "equipment_id": equipment_id_str or raw_eq_id,
                        "driver_name": driver_name,
                        "helper1_name": helper1_name,
                        "helper2_name": helper2_name,
                        "helper3_name": helper3_name,
                    },
                },
            )

        # Base completion info (date/time now auto-handled server-side)
        completion, _ = CompletionInfo.objects.update_or_create(
            service_request=service_request,
            defaults={
                "date_completed": timezone.now().date(),
                "time_required": "N/A",
                "witnessed_by_name": witnessed_name,
                "equipment": equipment,
                "declogger_no": declogger_no,
                "driver_name": driver_name,
                "helper1_name": helper1_name,
                "helper2_name": helper2_name,
                "helper3_name": helper3_name,
            },
        )

        # Witness name/signature come from the inspection step (submit_inspection), not this form.

        # Auto-generate computation
        _auto_generate_computation(service_request, request.user)

        messages.success(request, "Completion info saved. Computation letter generated.")
        return redirect("services:request_detail", pk=pk)

    existing = getattr(service_request, "completion_info", None)
    posted_initial = {
        "equipment_id": "",
        "driver_name": "",
        "helper1_name": "",
        "helper2_name": "",
        "helper3_name": "",
    }
    if existing:
        posted_initial = {
            "equipment_id": str(existing.equipment_id) if existing.equipment_id else "",
            "driver_name": existing.driver_name or "",
            "helper1_name": existing.helper1_name or "",
            "helper2_name": existing.helper2_name or "",
            "helper3_name": existing.helper3_name or "",
        }
        if not posted_initial["equipment_id"] and existing.declogger_no:
            match = ServiceEquipment.objects.filter(
                unit_number__iexact=existing.declogger_no.strip()
            ).first()
            if match:
                posted_initial["equipment_id"] = str(match.pk)
    extra_eq = None
    if posted_initial.get("equipment_id"):
        try:
            extra_eq = ServiceEquipment.objects.filter(
                pk=int(posted_initial["equipment_id"])
            ).first()
        except (ValueError, TypeError):
            pass
    service_equipment = _equipment_choices_for_form(extra_equipment=extra_eq)
    return render(
        request,
        "services/completion_form.html",
        {
            "sr": service_request,
            "inspection_detail": insp_detail,
            "personnel_drivers": personnel_drivers,
            "personnel_helpers": personnel_helpers,
            "service_equipment": service_equipment,
            "posted": posted_initial,
        },
    )


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

def _computation_letter_formal_context():
    """Treasurer / signatory lines for the printed computation letter (see settings)."""
    return {
        "letter_treasurer_name": settings.COMPUTATION_LETTER_TREASURER_NAME,
        "letter_treasurer_title": settings.COMPUTATION_LETTER_TREASURER_TITLE,
        "letter_signatory_name": settings.COMPUTATION_LETTER_SIGNATORY_NAME,
        "letter_signatory_title": settings.COMPUTATION_LETTER_SIGNATORY_TITLE,
        "letter_prepared_by_title": settings.COMPUTATION_LETTER_PREPARED_BY_TITLE,
    }


def _prepared_by_signature_absolute_url(request, computation):
    """Public URL for the letter/PDF only if the signature file exists in storage."""
    from .computation_flow import stored_filefield_exists

    try:
        f = getattr(computation, "prepared_by_signature", None)
        if not stored_filefield_exists(f):
            return None
        return request.build_absolute_uri(f.url)
    except Exception:
        return None


def _prepared_by_signature_fs_path(computation):
    """Local filesystem path for PDF embedding, or None if unavailable."""
    from .computation_flow import stored_filefield_exists

    if not stored_filefield_exists(getattr(computation, "prepared_by_signature", None)):
        return None
    try:
        p = computation.prepared_by_signature.path
    except (ValueError, NotImplementedError):
        return None
    if os.path.isfile(p):
        return p
    return None


def _signatory_signature_absolute_url(request, computation):
    from .computation_flow import stored_filefield_exists

    try:
        f = getattr(computation, "letter_signatory_signature", None)
        if not stored_filefield_exists(f):
            return None
        return request.build_absolute_uri(f.url)
    except Exception:
        return None


def _signatory_signature_fs_path(computation):
    from .computation_flow import stored_filefield_exists

    if not stored_filefield_exists(getattr(computation, "letter_signatory_signature", None)):
        return None
    try:
        p = computation.letter_signatory_signature.path
    except (ValueError, NotImplementedError):
        return None
    if os.path.isfile(p):
        return p
    return None


def _user_can_access_computation_letter(user, service_request) -> bool:
    """Who may open the computation letter URL (admin, owner consumer, or submitter). Staff use inspection only."""
    if user.is_admin():
        return True
    if service_request.consumer == user:
        return True
    if service_request.requested_by and service_request.requested_by == user:
        return True
    return False


def _user_can_finalize_computation(user) -> bool:
    """Only admins may preview draft letters, finalize, or download unfinalized PDFs."""
    return user.is_admin()


@login_required
def view_computation(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _user_can_access_computation_letter(request.user, service_request):
        _message_computation_letter_access_denied(request, service_request)
        return redirect("services:request_list")

    try:
        computation = service_request.computation
    except ObjectDoesNotExist:
        computation = None
    except DatabaseError:
        logger.exception("view_computation: database error loading computation")
        messages.error(
            request,
            "Could not load the computation. Run `python manage.py migrate`, restart the server, and try again.",
        )
        return redirect("services:request_detail", pk=pk)
    if not computation:
        messages.warning(request, _COMPUTATION_NOT_READY_MSG)
        return redirect("services:request_detail", pk=pk)

    if request.method == "POST":
        if not _user_can_finalize_computation(request.user):
            messages.error(
                request,
                "Only a CENRO administrator can finalize the computation letter and send it to the customer.",
            )
            return redirect("services:request_detail", pk=pk)
        action = request.POST.get("action") or "finalize"
        if action == "finalize":
            from .computation_flow import computation_finalize_blockers

            if not computation.ready_to_finalize:
                messages.error(
                    request,
                    "Save the computation with Save & Recalculate on the edit screen first, then return here to finalize and send.",
                )
                return redirect("services:view_computation", pk=pk)

            sig_prepared = request.FILES.get("prepared_by_signature")
            sig_signatory = request.FILES.get("letter_signatory_signature")
            if sig_prepared:
                computation.prepared_by_signature = sig_prepared
            if sig_signatory:
                computation.letter_signatory_signature = sig_signatory

            blockers = computation_finalize_blockers(
                service_request,
                computation,
                uploaded_prepared_signature=sig_prepared,
                uploaded_signatory_signature=sig_signatory,
            )
            if blockers:
                for msg in blockers:
                    messages.error(request, msg)
                _uf = []
                if sig_prepared:
                    _uf.append("prepared_by_signature")
                if sig_signatory:
                    _uf.append("letter_signatory_signature")
                if _uf:
                    _uf.append("updated_at")
                    computation.save(update_fields=_uf)
                return redirect("services:view_computation", pk=pk)

            computation.prepared_by = request.user
            computation.is_finalized = True
            computation.finalized_at = timezone.now()
            computation.ready_to_finalize = False
            computation.save()

            from dashboard.models import ServiceComputation

            computation.refresh_from_db()
            if computation.payment_status == ServiceComputation.PaymentStatus.FREE:
                service_request.status = ServiceRequest.Status.PAID
                service_request.payment_confirmed_at = timezone.now()
                service_request.fee_amount = computation.total_charge
                service_request.save(
                    update_fields=["status", "fee_amount", "payment_confirmed_at", "updated_at"]
                )
                free_msg = (
                    "Your computation letter is ready. No treasurer payment is required for this request "
                    "(public property within Bayawan City, BAWAD zero-charge eligibility, or other waived total). "
                    "You can view and download the letter; staff may schedule desludging next."
                )
            else:
                service_request.status = ServiceRequest.Status.COMPUTATION_SENT
                service_request.fee_amount = computation.total_charge
                service_request.save(update_fields=["status", "fee_amount", "updated_at"])
                free_msg = "Your computation letter is ready. You can now view and download it."

            # Notify the account owner (consumer) and, if different, the account that submitted the request.
            Notification.objects.create(
                user=service_request.consumer,
                message=free_msg,
                notification_type=Notification.NotificationType.COMPUTATION_READY,
                related_request=service_request,
            )
            if service_request.requested_by and service_request.requested_by != service_request.consumer:
                Notification.objects.create(
                    user=service_request.requested_by,
                    message=free_msg,
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

    computation.refresh_from_db()

    # Consumers / requested_by may only see the letter after finalization; only admins may preview drafts.
    if not _user_can_finalize_computation(request.user) and not computation.is_finalized:
        messages.info(request, "The computation is still being finalized by the administrator.")
        return redirect("services:request_detail", pk=pk)

    # Address without repeating barangay if it already appears at the end
    addr = (service_request.address or "").strip()
    brgy = (service_request.barangay or "").strip()
    if brgy and addr.endswith(brgy):
        address_display = addr
    else:
        address_display = f"{addr}, {brgy}" if brgy else addr

    prepared_by_signature_url = _prepared_by_signature_absolute_url(request, computation)
    signatory_signature_url = _signatory_signature_absolute_url(request, computation)

    can_finalize_letter = (
        _user_can_finalize_computation(request.user)
        and not computation.is_finalized
        and computation.ready_to_finalize
    )

    computation.recompute_letter_breakdown()

    ctx = {
        "sr": service_request,
        "comp": computation,
        "address_display": address_display,
        "prepared_by_signature_url": prepared_by_signature_url,
        "signatory_signature_url": signatory_signature_url,
        "can_finalize_letter": can_finalize_letter,
    }
    ctx.update(_computation_letter_formal_context())
    return render(request, "services/computation_letter.html", ctx)


@login_required
def download_computation_pdf(request, pk):
    """Generate and return the computation letter as a PDF download."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _user_can_access_computation_letter(request.user, service_request):
        _message_computation_letter_access_denied(request, service_request)
        return redirect("services:request_list")

    try:
        computation = service_request.computation
    except ObjectDoesNotExist:
        computation = None
    except DatabaseError:
        logger.exception("download_computation_pdf: database error loading computation")
        messages.error(
            request,
            "Could not load the computation. Run `python manage.py migrate`, restart the server, and try again.",
        )
        return redirect("services:request_detail", pk=pk)
    if not computation:
        messages.warning(request, _COMPUTATION_NOT_READY_MSG)
        return redirect("services:request_detail", pk=pk)

    if not _user_can_finalize_computation(request.user) and not computation.is_finalized:
        messages.info(request, "The computation is still being finalized by the administrator.")
        return redirect("services:request_detail", pk=pk)

    try:
        from xhtml2pdf import pisa
    except ImportError:
        messages.error(
            request,
            "PDF download is not enabled on this server (the xhtml2pdf library is missing). "
            "Ask your system administrator to install it, or use Print from the letter page in your browser.",
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

    prepared_by_signature_path = _prepared_by_signature_fs_path(computation)
    signatory_signature_path = _signatory_signature_fs_path(computation)

    pdf_font_family = _register_xhtml2pdf_unicode_font()
    pdf_body_font_stack = (
        f"{pdf_font_family}, Helvetica, Arial, sans-serif"
        if pdf_font_family
        else "Helvetica, Arial, sans-serif"
    )

    bayawan_logo_url = request.build_absolute_uri(static("img/bayawan_logo.png"))
    cenro_logo_url = request.build_absolute_uri(static("img/cenro_logo.png"))
    bagong_pilipinas_logo_url = request.build_absolute_uri(static("img/bagong_pilipinas_logo.png"))

    template = get_template("services/computation_letter_pdf.html")
    pdf_ctx = {
        "sr": service_request,
        "comp": computation,
        "address_display": address_display,
        "prepared_by_signature_path": prepared_by_signature_path,
        "signatory_signature_path": signatory_signature_path,
        "pdf_body_font_stack": pdf_body_font_stack,
        "bayawan_logo_url": bayawan_logo_url,
        "cenro_logo_url": cenro_logo_url,
        "bagong_pilipinas_logo_url": bagong_pilipinas_logo_url,
    }
    pdf_ctx.update(_computation_letter_formal_context())
    computation.recompute_letter_breakdown()
    html = template.render(pdf_ctx)
    result = io.BytesIO()
    pdf = pisa.pisaDocument(
        io.BytesIO(html.encode("utf-8")),
        result,
        encoding="utf-8",
    )
    if pdf.err:
        messages.error(
            request,
            f"The PDF could not be built from the computation letter (request #{service_request.pk}). "
            "Try again, or use your browser's print option on the letter page. If it keeps failing, contact support.",
        )
        return redirect("services:view_computation", pk=pk)

    filename = f"computation-ECO-{service_request.created_at.year}-{service_request.id:03d}.pdf"
    response = HttpResponse(result.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@role_required("ADMIN")
def edit_computation(request, pk):
    """Edit computation (admin only). Recalculates charges on save; finalize on the letter view."""
    from dashboard.forms import ServiceComputationForm

    service_request = get_object_or_404(ServiceRequest, pk=pk)
    try:
        computation = service_request.computation
    except ObjectDoesNotExist:
        computation = None
    except DatabaseError:
        logger.exception("edit_computation: database error loading ServiceComputation (run migrations?)")
        messages.error(
            request,
            "The database could not load this computation. If you updated the project, run "
            "`python manage.py migrate` in the project folder, restart the server, and try again.",
        )
        return redirect("services:request_detail", pk=pk)
    if not computation:
        messages.warning(request, _COMPUTATION_NOT_READY_MSG)
        return redirect("services:request_detail", pk=pk)

    def _apply_computation_distance_help(f):
        from dashboard.models import ConfigurableRate

        sr = service_request
        try:
            fk = ConfigurableRate.get("bayawan_resident_free_km", Decimal("10"))
            try:
                km_free = int(fk) if fk == fk.to_integral_value() else fk
            except Exception:
                km_free = fk
            if computation.qualifies_inside_public_bawad_program:
                f.fields["distance_km"].help_text = (
                    f"Public / BAWAD (inside Bayawan): first {km_free} km from CENRO office are not charged for travel. "
                    "Beyond that, distance travel (billable km × ₱20 × 2), wear & tear (20% of trucking + travel), and meals still apply; "
                    "trucking and tipping/septage stay waived."
                )
            elif (
                sr.is_within_bayawan
                and not computation.is_outside_bayawan
                and sr.consumer_is_bayawan_city_resident
            ):
                f.fields["distance_km"].help_text = (
                    f"Bayawan City resident + service within Bayawan: first {km_free} km are not charged "
                    "for distance travel (enter full km from CENRO; billing uses the distance above that)."
                )
            else:
                f.fields["distance_km"].help_text = (
                    "Distance from CENRO (whole km). Outside Bayawan: all kilometers are billable. "
                    "Inside Bayawan (private, not public/BAWAD waiver): first 10 km free only for Bayawan City residents "
                    "(profile municipality)."
                )
        except Exception:
            logger.exception("edit_computation: distance help text fallback")
            f.fields["distance_km"].help_text = (
                "Distance from CENRO (whole km). Billing uses configured free-km rules for your area and waivers."
            )

    form = ServiceComputationForm(instance=computation)
    form.fields.pop("charge_category", None)
    form.fields.pop("trips", None)
    form.fields.pop("is_outside_bayawan", None)
    _apply_computation_distance_help(form)

    if request.method == "POST":
        action = request.POST.get("action") or "save"
        form = ServiceComputationForm(request.POST, instance=computation)
        form.fields.pop("charge_category", None)
        form.fields.pop("trips", None)
        form.fields.pop("is_outside_bayawan", None)
        _apply_computation_distance_help(form)
        if form.is_valid():
            try:
                form.save()

                # Update signatures whenever new files are uploaded (for both Save and Finalize)
                sig = request.FILES.get("prepared_by_signature")
                if sig:
                    computation.prepared_by_signature = sig
                sig_en = request.FILES.get("letter_signatory_signature")
                if sig_en:
                    computation.letter_signatory_signature = sig_en

                service_request.fee_amount = computation.total_charge

                service_request.save(update_fields=["fee_amount"])
                computation.ready_to_finalize = True
                if action == "finalize":
                    messages.info(
                        request,
                        "Finalize and send is only on the computation letter. Your changes were saved — open the letter and use Finalize & Send when ready.",
                    )
                else:
                    messages.success(
                        request,
                        "Computation updated. Charges recalculated. Review the letter to finalize and send.",
                    )
                computation.save()
                from dashboard.models import ServiceComputation

                ServiceComputation.objects.filter(pk=computation.pk).update(ready_to_finalize=True)
            except DatabaseError:
                logger.exception("edit_computation: save failed (migrations needed?)")
                messages.error(
                    request,
                    "Could not save the computation (database error). Run `python manage.py migrate`, restart the server, and try again.",
                )
                return redirect("services:request_detail", pk=pk)
            return redirect("services:view_computation", pk=pk)

    prepared_by_signature_url = _prepared_by_signature_absolute_url(request, computation)
    signatory_signature_url = _signatory_signature_absolute_url(request, computation)

    return render(request, "services/computation_edit.html", {
        "form": form,
        "sr": service_request,
        "comp": computation,
        "prepared_by_signature_url": prepared_by_signature_url,
        "signatory_signature_url": signatory_signature_url,
    })


# ---------------------------------------------------------------------------
# Customer receipt upload
# ---------------------------------------------------------------------------

@login_required
def upload_receipt(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_act_on_request(request.user, service_request):
        _message_receipt_upload_denied(request)
        return redirect("services:request_list")

    if service_request.service_type == ServiceRequest.ServiceType.GRASS_CUTTING:
        if service_request.treasurer_receipt:
            messages.info(request, "A Treasurer payment receipt is already uploaded for this request.")
            return redirect("services:request_detail", pk=pk)
        # New workflow: Pending Payment. Legacy rows may still be Submitted / Under Review until upload.
        if service_request.status not in (
            ServiceRequest.Status.GRASS_PENDING_PAYMENT,
            ServiceRequest.Status.SUBMITTED,
            ServiceRequest.Status.UNDER_REVIEW,
        ):
            messages.error(
                request,
                f"Treasurer payment receipts for Grass Cutting can only be uploaded while the request is waiting for payment "
                f"or still being set up. Current status: {service_request.get_status_display()}. "
                "Open the request detail page to see the current step.",
            )
            return redirect("services:request_detail", pk=pk)

    if request.method == "POST":
        receipt = request.FILES.get("treasurer_receipt")
        if receipt:
            try:
                validate_customer_receipt(receipt)
            except ValidationError as e:
                messages.error(request, next(iter(e.messages)))
                return redirect("services:upload_receipt", pk=pk)
            if service_request.service_type == ServiceRequest.ServiceType.GRASS_CUTTING:
                with transaction.atomic():
                    service_request.treasurer_receipt = receipt
                    service_request.status = ServiceRequest.Status.GRASS_PAYMENT_AWAITING_VERIFICATION
                    service_request.save(update_fields=["treasurer_receipt", "status", "updated_at"])
                    _notify_admin_users(
                        (
                            f"Grass Cutting: payment receipt uploaded by {service_request.client_name} "
                            f"for request #{service_request.id}. Please verify and confirm or cancel."
                        ),
                        Notification.NotificationType.PAYMENT_UPLOADED,
                        service_request,
                    )
                messages.success(
                    request,
                    "Payment receipt uploaded. An administrator will verify it before your service can proceed.",
                )
            else:
                with transaction.atomic():
                    service_request.treasurer_receipt = receipt
                    service_request.status = ServiceRequest.Status.AWAITING_PAYMENT
                    service_request.save(update_fields=["treasurer_receipt", "status"])

                    computation = getattr(service_request, "computation", None)
                    if computation:
                        from dashboard.models import ServiceComputation
                        if computation.payment_status != ServiceComputation.PaymentStatus.FREE:
                            computation.payment_status = ServiceComputation.PaymentStatus.AWAITING_VERIFICATION
                            computation.save(update_fields=["payment_status"])

                    _notify_admin_users(
                        f"Payment receipt uploaded by {service_request.client_name} for request #{service_request.id}.",
                        Notification.NotificationType.PAYMENT_UPLOADED,
                        service_request,
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
        _message_inspection_fee_page_denied(request)
        return redirect("services:request_list")
    if service_request.service_type not in [
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ]:
        messages.error(
            request,
            "The inspection fee bill applies only to residential or commercial septage desludging. "
            "This request is for a different service type.",
        )
        return redirect("services:request_detail", pk=pk)
    if service_request.qualifies_public_bayawan_no_fees:
        messages.info(
            request,
            "No inspection fee applies — public property within Bayawan City. Open the request for next steps.",
        )
        return redirect("services:request_detail", pk=pk)
    return render(request, "services/inspection_fee_bill.html", {"sr": service_request, "amount": 150})


@login_required
def download_inspection_fee_bill_pdf(request, pk):
    """Generate and return the inspection fee bill as a PDF download."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_act_on_request(request.user, service_request):
        _message_inspection_fee_page_denied(request)
        return redirect("services:request_list")
    if service_request.service_type not in [
        ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
        ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
    ]:
        messages.error(
            request,
            "The inspection fee PDF applies only to residential or commercial septage desludging.",
        )
        return redirect("services:request_detail", pk=pk)
    if service_request.qualifies_public_bayawan_no_fees:
        messages.info(
            request,
            "No inspection fee applies — public property within Bayawan City.",
        )
        return redirect("services:request_detail", pk=pk)

    try:
        from xhtml2pdf import pisa
    except ImportError:
        messages.error(
            request,
            "PDF download is not enabled on this server (the xhtml2pdf library is missing). "
            "Use the on-screen bill and print from your browser, or ask an administrator to install xhtml2pdf.",
        )
        return redirect("services:inspection_fee_bill", pk=pk)

    template = get_template("services/inspection_fee_bill_pdf.html")
    bayawan_logo_url = request.build_absolute_uri(static("img/bayawan_logo.png"))
    cenro_logo_url = request.build_absolute_uri(static("img/cenro_logo.png"))
    bagong_pilipinas_logo_url = request.build_absolute_uri(static("img/bagong_pilipinas_logo.png"))
    html = template.render(
        {
            "sr": service_request,
            "amount": 150,
            "bayawan_logo_url": bayawan_logo_url,
            "cenro_logo_url": cenro_logo_url,
            "bagong_pilipinas_logo_url": bagong_pilipinas_logo_url,
        }
    )
    result = io.BytesIO()
    pdf = pisa.pisaDocument(
        io.BytesIO(html.encode("utf-8")),
        result,
        encoding="utf-8",
    )
    if pdf.err:
        messages.error(
            request,
            f"The inspection fee bill PDF could not be generated (request #{service_request.pk}). "
            "Try again or print the bill page from your browser.",
        )
        return redirect("services:inspection_fee_bill", pk=pk)

    filename = f"inspection-fee-bill-{service_request.id:03d}.pdf"
    response = HttpResponse(result.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def upload_inspection_fee_receipt(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_act_on_request(request.user, service_request):
        _message_inspection_fee_page_denied(request)
        return redirect("services:request_list")

    allowed_upload_statuses = (
        ServiceRequest.Status.INSPECTION_FEE_DUE,
        ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION,
    )
    if service_request.status not in allowed_upload_statuses:
        messages.error(
            request,
            f"You can upload an inspection fee receipt only while the request is 'Inspection Fee Due' or "
            f"awaiting verification. Current status: {service_request.get_status_display()}. "
            "Open the request detail page for the next step.",
        )
        return redirect("services:request_detail", pk=pk)

    if service_request.qualifies_public_bayawan_no_fees:
        messages.info(
            request,
            "No inspection fee receipt is required — public property within Bayawan City.",
        )
        return redirect("services:request_detail", pk=pk)

    if request.method == "POST":
        receipt = request.FILES.get("inspection_fee_receipt")
        if receipt:
            try:
                validate_customer_receipt(receipt)
            except ValidationError as e:
                messages.error(request, next(iter(e.messages)))
                return redirect("services:upload_inspection_fee", pk=pk)
            with transaction.atomic():
                old_receipt = service_request.inspection_fee_receipt
                if old_receipt:
                    old_receipt.delete(save=False)
                service_request.inspection_fee_receipt = receipt
                service_request.status = ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION
                service_request.save(update_fields=["inspection_fee_receipt", "status"])

                _notify_admin_users(
                    (
                        f"Inspection fee receipt uploaded by {service_request.client_name} "
                        f"for request #{service_request.id}."
                    ),
                    Notification.NotificationType.PAYMENT_UPLOADED,
                    service_request,
                )
            messages.success(request, "Inspection fee receipt uploaded. Waiting for admin verification.")
        else:
            messages.error(request, "Please select a file to upload.")
        return redirect("services:request_detail", pk=pk)

    is_reupload = service_request.status == ServiceRequest.Status.INSPECTION_FEE_AWAITING_VERIFICATION
    return render(
        request,
        "services/upload_inspection_fee.html",
        {"sr": service_request, "is_reupload": is_reupload},
    )


@login_required
def view_inspection_fee_receipt(request, pk):
    """Serve the uploaded inspection fee receipt so admins can view it reliably."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_view_request_uploaded_files(request.user, service_request):
        _message_uploaded_file_access_denied(request, service_request, "the inspection fee receipt")
        return redirect("services:request_list")
    if not service_request.inspection_fee_receipt:
        messages.error(request, "No inspection fee receipt uploaded for this request.")
        return redirect("services:request_detail", pk=pk)

    try:
        receipt_file = service_request.inspection_fee_receipt.open("rb")
    except FileNotFoundError:
        messages.error(request, "The uploaded inspection fee receipt file could not be found on the server.")
        return redirect("services:request_detail", pk=pk)

    ctype, _ = mimetypes.guess_type(service_request.inspection_fee_receipt.name or "")
    if not ctype:
        ctype = "application/octet-stream"
    return FileResponse(receipt_file, as_attachment=False, content_type=ctype)


@login_required
def view_bawad_proof(request, pk):
    """Serve BAWAD affiliation proof with the same access rules as other request uploads."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_view_request_uploaded_files(request.user, service_request):
        _message_uploaded_file_access_denied(request, service_request, "the BAWAD affiliation proof")
        return redirect("services:request_list")
    if not service_request.bawad_proof:
        messages.error(request, "No BAWAD affiliation proof was uploaded for this request.")
        return redirect("services:request_detail", pk=pk)
    try:
        proof_file = service_request.bawad_proof.open("rb")
    except FileNotFoundError:
        messages.error(request, "The BAWAD proof file could not be found on the server.")
        return redirect("services:request_detail", pk=pk)
    ctype, _ = mimetypes.guess_type(service_request.bawad_proof.name or "")
    if not ctype:
        ctype = "application/octet-stream"
    return FileResponse(proof_file, as_attachment=False, content_type=ctype)


def _file_response_for_client_signature(service_request):
    """
    Open client_signature from storage. Tries alternate relative paths for legacy rows
    where the filename incorrectly included an extra 'client_signatures/' segment.
    """
    field = service_request.client_signature
    if not field or not field.name:
        return None
    storage = field.storage
    base = os.path.basename(field.name)

    def _guess_ct(path_hint: str) -> str:
        ctype, _ = mimetypes.guess_type(path_hint or base or "")
        if not ctype or ctype == "application/octet-stream":
            ctype = "image/png"
        return ctype

    try:
        handle = field.open("rb")
        return FileResponse(handle, as_attachment=False, content_type=_guess_ct(field.name))
    except FileNotFoundError:
        pass

    for rel in (
        f"client_signatures/{base}",
        f"client_signatures/client_signatures/{base}",
    ):
        if storage.exists(rel):
            try:
                handle = storage.open(rel, "rb")
                return FileResponse(handle, as_attachment=False, content_type=_guess_ct(base))
            except OSError:
                continue
    return None


@login_required
def view_client_signature(request, pk):
    """Serve client signature with the same access rules as other request uploads (modal-friendly URL)."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_view_request_uploaded_files(request.user, service_request):
        _message_uploaded_file_access_denied(request, service_request, "the client signature")
        return redirect("services:request_list")
    if not service_request.client_signature:
        messages.error(request, "No client signature was uploaded for this request.")
        return redirect("services:request_detail", pk=pk)

    response = _file_response_for_client_signature(service_request)
    if response is None:
        messages.error(request, "The signature file could not be found on the server.")
        return redirect("services:request_detail", pk=pk)
    return response


@login_required
def view_location_photo(request, pk, slot):
    """Serve location photos with the same access rules as payment receipts (login + role)."""
    if slot not in (1, 2):
        raise Http404()
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_view_request_uploaded_files(request.user, service_request):
        _message_uploaded_file_access_denied(request, service_request, "this location photo")
        return redirect("services:request_list")
    field = service_request.location_photo_1 if slot == 1 else service_request.location_photo_2
    if not field:
        messages.error(
            request,
            f"No location photo #{slot} was uploaded for request #{service_request.pk}.",
        )
        return redirect("services:request_detail", pk=pk)
    try:
        photo_file = field.open("rb")
    except FileNotFoundError:
        messages.error(request, "The location photo file could not be found on the server.")
        return redirect("services:request_detail", pk=pk)
    ctype, _ = mimetypes.guess_type(field.name or "")
    if not ctype or ctype == "application/octet-stream":
        ctype = "image/jpeg"
    return FileResponse(photo_file, as_attachment=False, content_type=ctype)


def _can_view_request_uploaded_files(user, service_request):
    """Who may open uploaded receipts and location photos (served outside /media/)."""
    if _can_act_on_request(user, service_request):
        return True
    if user.is_staff_member() and service_request.assigned_inspector_id == user.id:
        return True
    # Grass cutting is not inspector-assigned; office admins/staff still need to review payment proof.
    if service_request.service_type == ServiceRequest.ServiceType.GRASS_CUTTING and (
        user.is_admin() or user.is_staff_member()
    ):
        return True
    return False


@login_required
def view_treasurer_receipt(request, pk):
    """Serve the treasurer payment receipt reliably (avoids broken /media/ URLs in dev)."""
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    if not _can_view_request_uploaded_files(request.user, service_request):
        _message_uploaded_file_access_denied(request, service_request, "the Treasurer payment receipt")
        return redirect("services:request_list")
    if not service_request.treasurer_receipt:
        messages.error(request, "No payment receipt uploaded for this request.")
        return redirect("services:request_detail", pk=pk)

    try:
        receipt_file = service_request.treasurer_receipt.open("rb")
    except FileNotFoundError:
        messages.error(request, "The uploaded payment receipt file could not be found on the server.")
        return redirect("services:request_detail", pk=pk)

    ctype, _ = mimetypes.guess_type(service_request.treasurer_receipt.name or "")
    if not ctype:
        ctype = "application/octet-stream"
    return FileResponse(receipt_file, as_attachment=False, content_type=ctype)


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


@login_required
@require_POST
def consumer_cancel_request(request, pk):
    """
    Consumer or original submitter (request-for-other) cancels the request.
    Requires POST field ``confirm_text`` exactly equal to ``DELETE``.
    """
    user = request.user
    if user.is_admin() or user.is_staff_member():
        messages.error(
            request,
            "Office accounts cannot use customer cancel — use the administrator tools on this page instead.",
        )
        return redirect("services:request_detail", pk=pk)

    service_request = get_object_or_404(ServiceRequest, pk=pk)
    is_party = service_request.consumer_id == user.id or (
        service_request.requested_by_id and service_request.requested_by_id == user.id
    )
    if not is_party:
        messages.error(request, "You can only cancel requests tied to your own account.")
        return redirect("services:request_list")

    if service_request.status in _CONSUMER_CANCEL_BLOCKED_STATUSES:
        messages.error(
            request,
            "This request can no longer be cancelled online. For changes at this stage, contact CENRO Bayawan directly.",
        )
        return redirect("services:request_detail", pk=pk)

    if (request.POST.get("confirm_text") or "").strip() != "DELETE":
        messages.error(
            request,
            'Cancellation was not confirmed. Type the word DELETE (all caps) exactly as shown, then try again.',
        )
        return redirect("services:request_detail", pk=pk)

    who = "customer"
    if service_request.requested_by_id == user.id and service_request.consumer_id != user.id:
        who = "submitter"

    note_line = (
        f"{ServiceRequest.CUSTOMER_CANCELLED_NOTE_PREFIX} "
        f"Cancelled by portal user ({who}, user id {user.id})."
    )
    new_notes = ((service_request.notes or "").strip() + "\n" + note_line).strip()

    with transaction.atomic():
        service_request.status = ServiceRequest.Status.CANCELLED
        service_request.notes = new_notes
        service_request.assigned_inspector = None
        service_request.inspection_date = None
        service_request.save(
            update_fields=[
                "status",
                "notes",
                "assigned_inspector",
                "inspection_date",
                "updated_at",
            ]
        )

    type_label = service_request.get_service_type_display()
    _notify_admin_users(
        f"Request #{service_request.id} ({type_label}, {service_request.client_name}) was cancelled by the customer in the portal.",
        Notification.NotificationType.STATUS_CHANGE,
        service_request,
    )

    if service_request.consumer_id == user.id:
        if service_request.requested_by_id and service_request.requested_by_id != user.id:
            Notification.objects.create(
                user_id=service_request.requested_by_id,
                message=(
                    f"Request #{service_request.id} for {service_request.client_name} was cancelled by the registered customer."
                )[:500],
                notification_type=Notification.NotificationType.STATUS_CHANGE,
                related_request=service_request,
            )
    else:
        Notification.objects.create(
            user_id=service_request.consumer_id,
            message=(
                f"Your {type_label} request #{service_request.id} was cancelled by the person who submitted it on your behalf."
            )[:500],
            notification_type=Notification.NotificationType.STATUS_CHANGE,
            related_request=service_request,
        )

    messages.success(request, "Your request has been cancelled.")
    return redirect("services:request_detail", pk=pk)


# ---------------------------------------------------------------------------
# Mark request complete
# ---------------------------------------------------------------------------

@login_required
def complete_request(request, pk):
    service_request = get_object_or_404(ServiceRequest, pk=pk)
    user = request.user

    if service_request.service_type == ServiceRequest.ServiceType.GRASS_CUTTING:
        messages.error(
            request,
            "Grass Cutting requests are marked completed only after payment verification by an administrator.",
        )
        return redirect("services:request_detail", pk=pk)

    is_office = user.is_admin() or user.is_staff_member()
    is_customer_party = service_request.consumer_id == user.id or (
        service_request.requested_by_id and service_request.requested_by_id == user.id
    )

    if not is_office and not is_customer_party:
        messages.error(request, "You do not have permission to mark this request as completed.")
        return redirect("services:request_detail", pk=pk)

    if not is_office:
        if service_request.status != ServiceRequest.Status.DESLUDGING_SCHEDULED:
            messages.error(
                request,
                "You can mark this request as completed only after desludging has been scheduled.",
            )
            return redirect("services:request_detail", pk=pk)

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
            # Always open the related request so admins are not dropped on a list tab (e.g. Pending).
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
    return JsonResponse(
        {"ok": False, "error": "This action requires POST (use the Mark all read control in the app)."},
        status=405,
    )
