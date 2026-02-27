import re

from django import forms

from .models import ServiceRequest
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

    client_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Client / Establishment Name"}),
        label="Client / Establishment Name",
    )
    request_date = forms.DateField(
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        label="Request Date",
    )
    address = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "House number, Street name, near [Landmark] (optional)",
        }),
        label="Complete Address",
    )
    barangay = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "readonly": "readonly"}),
        help_text="Auto-detected from the pin location.",
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

    def clean_contact_number(self):
        num = self.cleaned_data["contact_number"]
        cleaned = re.sub(r"[\s\-()]", "", num)
        if not re.match(r"^(\+63|0)?9\d{9}$", cleaned):
            raise forms.ValidationError("Enter a valid Philippine mobile number (e.g. 09XX-XXX-XXXX).")
        return cleaned

    def clean(self):
        cleaned = super().clean()
        lat = cleaned.get("gps_latitude")
        lon = cleaned.get("gps_longitude")
        if lat is None or lon is None:
            raise forms.ValidationError("Please select your location on the map.")

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

        cleaned["barangay"] = detected

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
