import re
from decimal import Decimal

from django import forms
from django.core.files.uploadedfile import UploadedFile

from .models import ServiceRequest

LOCATION_PHOTO_ALLOWED_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
)
LOCATION_PHOTO_MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20MB

LOCATION_PHOTO_ACCEPT_HTML = (
    "image/jpeg,image/jpg,image/png,image/webp,image/gif,image/bmp,image/tiff,.tif,.tiff"
)

RECEIPT_UPLOAD_ALLOWED_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
)
RECEIPT_UPLOAD_MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20MB


def validate_location_photo(uploaded_file: UploadedFile) -> None:
    if not uploaded_file:
        return
    name = (uploaded_file.name or "").lower()
    if not any(name.endswith(ext) for ext in LOCATION_PHOTO_ALLOWED_EXTENSIONS):
        raise forms.ValidationError(
            "Only image files are allowed (JPG, JPEG, PNG, WEBP, GIF, BMP, or TIFF)."
        )
    if uploaded_file.size > LOCATION_PHOTO_MAX_SIZE_BYTES:
        raise forms.ValidationError(
            f"File size must be 20MB or less (current: {uploaded_file.size / (1024*1024):.1f}MB)."
        )


def validate_customer_receipt(uploaded_file: UploadedFile) -> None:
    """Treasurer / payment receipts and inspection fee receipts (images or PDF)."""
    if not uploaded_file:
        raise forms.ValidationError("Please select a file to upload.")
    name = (uploaded_file.name or "").lower()
    if not any(name.endswith(ext) for ext in RECEIPT_UPLOAD_ALLOWED_EXTENSIONS):
        raise forms.ValidationError(
            "Allowed types: PDF, JPG, JPEG, PNG, WEBP, GIF, BMP, or TIFF (max 20MB each)."
        )
    if uploaded_file.size > RECEIPT_UPLOAD_MAX_SIZE_BYTES:
        raise forms.ValidationError(
            f"File must be 20MB or less (current: {uploaded_file.size / (1024*1024):.1f}MB)."
        )
from .geocode import address_in_service_area, extract_barangay, reverse_geocode_osm
from .location import detect_barangay_for_point, nearest_barangay, within_service_bounds


class ServiceRequestForm(forms.ModelForm):
    class Meta:
        model = ServiceRequest
        fields = ["barangay", "address", "service_type", "request_date", "notes"]
        widgets = {
            "barangay": forms.TextInput(attrs={"class": "form-control"}),
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "service_type": forms.Select(attrs={"class": "form-control"}),
            "request_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class ServiceRequestStep1Form(forms.Form):
    """Step 1 -- Service Type Selection"""

    SERVICE_CHOICES = [
        ("", "-- Select Service Type --"),
        ("RESIDENTIAL_DESLUDGING", "Residential Septage Desludging"),
        ("COMMERCIAL_DESLUDGING", "Commercial Septage Desludging"),
        ("GRASS_CUTTING", "Grass Cutting"),
    ]

    service_type = forms.ChoiceField(
        choices=SERVICE_CHOICES,
        widget=forms.Select(attrs={"class": "form-control"}),
        label="Select Service Type",
    )


class ServiceRequestStep2Form(forms.Form):
    """Step 2 -- Customer Request Form"""

    LOCATION_MODE_PIN = "PIN"
    LOCATION_MODE_TEXT = "TEXT"
    REQUEST_FOR_OWNER = "owner"
    REQUEST_FOR_OTHER = "other"

    request_for = forms.ChoiceField(
        choices=[
            (REQUEST_FOR_OWNER, "I am the account owner"),
            (REQUEST_FOR_OTHER, "Requesting for another person"),
        ],
        initial=REQUEST_FOR_OWNER,
        widget=forms.HiddenInput(attrs={"id": "id_request_for"}),
        required=False,
    )

    client_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Client / Establishment Name"}),
        label="Client / Establishment Name",
    )
    request_date = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        label="Request Date",
    )
    location_mode = forms.ChoiceField(
        choices=[
            (LOCATION_MODE_PIN, "Pin on Map"),
            (LOCATION_MODE_TEXT, "Type Address"),
        ],
        initial=LOCATION_MODE_PIN,
        widget=forms.HiddenInput(attrs={"id": "id_location_mode"}),
    )
    address = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "House number, Street name, near [Landmark] (optional)",
        }),
        label="Complete Address / Landmark",
    )
    barangay = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        label="Barangay",
    )
    gps_latitude = forms.DecimalField(required=False, widget=forms.HiddenInput())
    gps_longitude = forms.DecimalField(required=False, widget=forms.HiddenInput())

    contact_number = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "09XX-XXX-XXXX"}),
        label="Contact Number",
    )
    connected_to_bawad = forms.ChoiceField(
        choices=[("YES", "BAWAD Connected"), ("NO", "Other Source")],
        widget=forms.RadioSelect(attrs={"class": "bawad-radio"}),
        label="Water Source Connection:",
        initial="NO",
        required=False,
    )
    bawad_proof = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"class": "form-control"}),
        label="Proof of BAWAD Affiliation",
    )
    public_private = forms.ChoiceField(
        choices=[("PUBLIC", "Public"), ("PRIVATE", "Private")],
        widget=forms.RadioSelect(attrs={"class": "pub-priv-radio"}),
        label="Property type:",
        initial="PRIVATE",
    )
    client_signature = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"class": "form-control"}),
        label="Client Signature (upload image)",
    )
    # Optional base64-encoded image produced by the in-browser signature pad.
    client_signature_data = forms.CharField(required=False, widget=forms.HiddenInput())
    location_photo_1 = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={
            "class": "form-control location-photo-input",
            "accept": LOCATION_PHOTO_ACCEPT_HTML,
        }),
        label="Location photo 1",
    )
    location_photo_2 = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={
            "class": "form-control location-photo-input",
            "accept": LOCATION_PHOTO_ACCEPT_HTML,
        }),
        label="Location photo 2",
    )

    def __init__(self, *args, **kwargs):
        """
        Optionally accept `service_type` (from step 1/session) so that
        validation can depend on whether this is a declogging or grass-cutting request.
        """
        self.service_type = kwargs.pop("service_type", None)
        # Session-staged file path from a prior step-2 submit (multipart forms do not resend files).
        self.existing_bawad_proof_temp = kwargs.pop("existing_bawad_proof_temp", None) or ""
        super().__init__(*args, **kwargs)

    def clean_request_date(self):
        # Weekend requests are allowed; keep user-selected date unchanged.
        return self.cleaned_data["request_date"]

    def clean_contact_number(self):
        num = self.cleaned_data["contact_number"]
        cleaned = re.sub(r"[\s\-()]", "", num)
        if not re.match(r"^(\+63|0)?9\d{9}$", cleaned):
            raise forms.ValidationError("Enter a valid Philippine mobile number (e.g. 09XX-XXX-XXXX).")
        return cleaned

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("location_mode") or self.LOCATION_MODE_PIN

        if mode == self.LOCATION_MODE_TEXT:
            barangay = (cleaned.get("barangay") or "").strip()
            address = (cleaned.get("address") or "").strip()
            if not barangay:
                self.add_error("barangay", "Barangay is required when using Type Address.")
            if not address:
                self.add_error("address", "Complete Address / Landmark is required when using Type Address.")
            cleaned["gps_latitude"] = None
            cleaned["gps_longitude"] = None
            for key in ("location_photo_1", "location_photo_2"):
                f = self.files.get(key) or cleaned.get(key)
                if f:
                    try:
                        validate_location_photo(f)
                    except forms.ValidationError as e:
                        self.add_error(key, e)
        else:
            lat = cleaned.get("gps_latitude")
            lon = cleaned.get("gps_longitude")
            if lat is None or lon is None:
                self.add_error(None, "Please select your location on the map.")
                return cleaned

            lat_f = float(lat)
            lon_f = float(lon)
            if not within_service_bounds(lat_f, lon_f):
                self.add_error(
                    None,
                    "Service locations are only accepted within Bayawan City, the Municipality of "
                    "Santa Catalina, or the Municipality of Basay. Move the map pin into an allowed "
                    "area or use Type Address.",
                )
                return cleaned

            detected = detect_barangay_for_point(lat_f, lon_f)
            if not detected:
                data = reverse_geocode_osm(lat_f, lon_f)
                if data:
                    address = data.get("address") or {}
                    display_name = data.get("display_name")
                    within = address_in_service_area(address, display_name)
                    if within:
                        detected = extract_barangay(address) or nearest_barangay(lat_f, lon_f)
                if not detected and within_service_bounds(lat_f, lon_f):
                    detected = nearest_barangay(lat_f, lon_f)
                if not detected:
                    detected = None

            if detected:
                cleaned["barangay"] = detected
            else:
                cleaned["barangay"] = cleaned.get("barangay") or ""

        # Connected to BAWAD + proof:
        # Only enforce this for declogging-type services (residential/commercial desludging).
        service_type = self.service_type or ""
        is_declogging = service_type in {
            ServiceRequest.ServiceType.RESIDENTIAL_DESLUDGING,
            ServiceRequest.ServiceType.COMMERCIAL_DESLUDGING,
        }

        if is_declogging:
            # Treat missing selection as "NO" so the field does not block submission,
            # especially for locations outside Bayawan where we auto-set to non-BAWAD.
            bawad = cleaned.get("connected_to_bawad") or "NO"
            cleaned["connected_to_bawad"] = bawad
            has_new_bawad = bool(cleaned.get("bawad_proof") or self.files.get("bawad_proof"))
            has_staged_bawad = bool((self.existing_bawad_proof_temp or "").strip())
            if bawad == "YES" and not has_new_bawad and not has_staged_bawad:
                self.add_error("bawad_proof", "Please upload proof of BAWAD affiliation.")
        else:
            # Grass cutting and other non-declogging services should not depend on BAWAD details.
            cleaned["connected_to_bawad"] = "NO"
            cleaned["bawad_proof"] = None

        return cleaned


class ServiceRequestStep3Form(forms.Form):
    """Step 3 -- Review & Confirmation"""

    terms = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="I confirm that the information provided is correct.",
    )


GRASSCUTTING_RATE_PER_HOUR = 40


class GrasscuttingApplicationForm(forms.Form):
    """Grasscutting Services Application Form (sections from paper form)."""

    date = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        label="Date",
    )
    date_of_grass_cutting = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        label="Date of Grass Cutting",
    )
    designated_time = forms.TimeField(
        widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}, format="%H:%M"),
        label="Designated Time",
        input_formats=["%H:%M", "%I:%M %p", "%I:%M%p", "%H:%M:%S"],
    )
    place_of_grass_cutting = forms.CharField(
        max_length=500,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Place of Grass Cutting"}),
        label="Place of Grass Cutting",
    )
    signature_over_printed_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        label="Signature over printed name",
    )
    contact_number = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "09XX-XXX-XXXX"}),
        label="Contact Number",
    )
    address = forms.CharField(
        max_length=500,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        label="Address",
    )
    number_of_personnel = forms.IntegerField(
        min_value=1,
        max_value=50,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1, "id": "id_number_of_personnel"}),
        label="Number of Personnel/s",
    )
    number_of_hours = forms.DecimalField(
        min_value=Decimal("0.5"),
        max_value=Decimal("24"),
        decimal_places=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0.5", "step": "0.5", "id": "id_number_of_hours"}),
        label="Number of Hour/s",
    )

    def clean_number_of_personnel(self):
        val = self.cleaned_data.get("number_of_personnel")
        if val is not None and (not isinstance(val, int) or val < 1):
            raise forms.ValidationError("Enter a positive number (at least 1).")
        return val

    def clean_number_of_hours(self):
        val = self.cleaned_data.get("number_of_hours")
        if val is not None:
            try:
                h = float(val)
                if h <= 0:
                    raise forms.ValidationError("Enter a positive number of hours.")
                if h > 24:
                    raise forms.ValidationError("Hours cannot exceed 24.")
            except (TypeError, ValueError):
                raise forms.ValidationError("Enter a valid number.")
        return val

    def clean_contact_number(self):
        import re
        num = (self.cleaned_data.get("contact_number") or "").strip()
        cleaned = re.sub(r"[\s\-()]", "", num)
        if not re.match(r"^(\+63|0)?9\d{9}$", cleaned):
            raise forms.ValidationError("Enter a valid Philippine mobile number (e.g. 09XX-XXX-XXXX).")
        return cleaned


class GrasscuttingAdminEditForm(forms.Form):
    """Admin edit form for Grass Cutting request: only date, personnel, hours + required remarks."""

    date_of_grass_cutting = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        label="Date of Grass Cutting",
    )
    number_of_personnel = forms.IntegerField(
        min_value=1,
        max_value=50,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        label="Number of Personnel/s",
    )
    number_of_hours = forms.DecimalField(
        min_value=Decimal("0.5"),
        max_value=Decimal("24"),
        decimal_places=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0.5", "step": "0.5"}),
        label="Number of Hour/s",
    )
    remarks = forms.CharField(
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4, "placeholder": "Explain why these values were changed (required)."}),
        label="Remarks (reason for change)",
        required=True,
    )

    def clean_remarks(self):
        remarks = (self.cleaned_data.get("remarks") or "").strip()
        if not remarks:
            raise forms.ValidationError("Remarks are required when saving changes.")
        return remarks
