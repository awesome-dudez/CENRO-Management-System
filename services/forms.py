import re

from django import forms
from django.core.files.uploadedfile import UploadedFile

from .models import ServiceRequest

LOCATION_PHOTO_ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
LOCATION_PHOTO_MAX_SIZE_BYTES = 5 * 1024 * 1024  # 5MB


def validate_location_photo(uploaded_file: UploadedFile) -> None:
    if not uploaded_file:
        return
    name = (uploaded_file.name or "").lower()
    if not any(name.endswith(ext) for ext in LOCATION_PHOTO_ALLOWED_EXTENSIONS):
        raise forms.ValidationError(
            "Only image files are allowed (JPG, JPEG, PNG, WEBP)."
        )
    if uploaded_file.size > LOCATION_PHOTO_MAX_SIZE_BYTES:
        raise forms.ValidationError(
            f"File size must be 5MB or less (current: {uploaded_file.size / (1024*1024):.1f}MB)."
        )
from .geocode import address_in_bayawan, extract_barangay, reverse_geocode_osm
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
        choices=[("NO", "No"), ("YES", "Yes")],
        widget=forms.RadioSelect(attrs={"class": "bawad-radio"}),
        label="Connected to BAWAD?",
        initial="NO",
    )
    bawad_proof = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"class": "form-control"}),
        label="Proof of BAWAD Affiliation",
    )
    public_private = forms.ChoiceField(
        choices=[("PRIVATE", "Private"), ("PUBLIC", "Public")],
        widget=forms.RadioSelect(attrs={"class": "pub-priv-radio"}),
        label="Public / Private",
        initial="PRIVATE",
    )
    client_signature = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"class": "form-control"}),
        label="Client Signature (upload image)",
    )
    location_photo_1 = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={
            "class": "form-control location-photo-input",
            "accept": "image/jpeg,image/jpg,image/png,image/webp",
        }),
        label="Location photo 1",
    )
    location_photo_2 = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={
            "class": "form-control location-photo-input",
            "accept": "image/jpeg,image/jpg,image/png,image/webp",
        }),
        label="Location photo 2",
    )

    def clean_request_date(self):
        from .business_days import next_business_day
        d = self.cleaned_data["request_date"]
        return next_business_day(d)

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
            detected = detect_barangay_for_point(lat_f, lon_f)
            if not detected:
                data = reverse_geocode_osm(lat_f, lon_f)
                if data:
                    address = data.get("address") or {}
                    display_name = data.get("display_name")
                    within = address_in_bayawan(address, display_name)
                    if within:
                        detected = extract_barangay(address) or nearest_barangay(lat_f, lon_f)
                if not detected and within_service_bounds(lat_f, lon_f):
                    detected = nearest_barangay(lat_f, lon_f)
                if not detected:
                    detected = "Outside Bayawan City"

            cleaned["barangay"] = detected or cleaned.get("barangay") or ""

        bawad = cleaned.get("connected_to_bawad")
        if bawad == "YES" and not cleaned.get("bawad_proof") and not self.files.get("bawad_proof"):
            self.add_error("bawad_proof", "Please upload proof of BAWAD affiliation.")

        return cleaned


class ServiceRequestStep3Form(forms.Form):
    """Step 3 -- Review & Confirmation"""

    terms = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="I confirm that the information provided is correct.",
    )
