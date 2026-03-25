from django import forms
from django.conf import settings
from decimal import Decimal
import re
from dashboard.models import ServiceComputation, DecloggingApplication, ChargeCategory
from services.models import ServiceRequest


class ServiceComputationForm(forms.ModelForm):
    """Form for computing service charges"""

    class Meta:
        model = ServiceComputation
        fields = [
            'charge_category',
            'cubic_meters',
            'trips',
            'personnel_count',
            'is_outside_bayawan',
            'distance_km',
            'payment_status',
        ]
        widgets = {
            'charge_category': forms.Select(attrs={'class': 'form-control'}),
            'cubic_meters': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'placeholder': 'Cubic meters',
            }),
            'trips': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'personnel_count': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
            'is_outside_bayawan': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'distance_km': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'placeholder': 'Distance in km',
            }),
            'payment_status': forms.Select(attrs={'class': 'form-control'}),
        }


class DecloggingApplicationForm(forms.ModelForm):
    """Form for declogging service applications"""
    
    class Meta:
        model = DecloggingApplication
        fields = [
            'applicant_name',
            'applicant_signature',
            'applicant_sign_date',
            'cenro_representative',
            'cenro_signature',
            'cenro_sign_date',
        ]
        widgets = {
            'applicant_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Full Name',
            }),
            'applicant_signature': forms.FileInput(attrs={'class': 'form-control'}),
            'applicant_sign_date': forms.DateInput(attrs={
                'class': 'form-control',
                'type': 'date',
            }),
            'cenro_representative': forms.Select(attrs={'class': 'form-control'}),
            'cenro_signature': forms.FileInput(attrs={'class': 'form-control'}),
            'cenro_sign_date': forms.DateInput(attrs={
                'class': 'form-control',
                'type': 'date',
            }),
        }


class QuickComputationForm(forms.Form):
    """Quick form for on-the-fly computation"""
    
    CATEGORY_CHOICES = [
        ('RESIDENTIAL', 'Residential'),
        ('COMMERCIAL', 'Commercial'),
    ]
    
    LOCATION_CHOICES = [
        ('inside', 'Inside Bayawan'),
        ('outside', 'Outside Bayawan'),
    ]
    
    category = forms.ChoiceField(choices=CATEGORY_CHOICES, widget=forms.Select(attrs={'class': 'form-control'}))
    location = forms.ChoiceField(choices=LOCATION_CHOICES, widget=forms.Select(attrs={'class': 'form-control'}))
    cubic_meters = forms.DecimalField(
        min_value=Decimal('0'),
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.01',
            'min': '0',
            'placeholder': 'Cubic meters',
        })
    )
    distance_km = forms.DecimalField(
        required=False,
        min_value=Decimal('0'),
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.01',
            'min': '0',
            'placeholder': 'Distance (km)',
        })
    )
    personnel_count = forms.IntegerField(
        min_value=1,
        max_value=30,
        initial=4,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
    )
    connected_to_bawad = forms.ChoiceField(
        choices=[("NO", "No"), ("YES", "Yes")],
        initial="NO",
        widget=forms.Select(attrs={'class': 'form-control'}),
        label="BAWAD member",
    )
    public_private = forms.ChoiceField(
        choices=ServiceRequest.PublicPrivate.choices,
        initial=ServiceRequest.PublicPrivate.PRIVATE,
        widget=forms.Select(attrs={'class': 'form-control'}),
        label="Public / Private",
    )
    bawad_prior_used_m3 = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        initial=Decimal("0"),
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.01',
            'min': '0',
            'placeholder': '0',
        }),
        label="BAWAD prior m³ (cycle)",
    )

    def clean_cubic_meters(self):
        value = self.cleaned_data.get('cubic_meters')
        if value is not None and value < 0:
            raise forms.ValidationError("Cubic meters cannot be negative.")
        return value

    def clean_distance_km(self):
        value = self.cleaned_data.get('distance_km')
        if value in (None, ''):
            return Decimal('0')
        if value < 0:
            raise forms.ValidationError("Distance cannot be negative.")
        return value

    def clean(self):
        cleaned = super().clean()
        location = cleaned.get("location")
        if location == "outside":
            cleaned["connected_to_bawad"] = "NO"
            cleaned["bawad_prior_used_m3"] = Decimal("0")
        elif cleaned.get("connected_to_bawad") != "YES":
            cleaned["bawad_prior_used_m3"] = Decimal("0")
        return cleaned

    def clean_bawad_prior_used_m3(self):
        value = self.cleaned_data.get("bawad_prior_used_m3")
        if value in (None, ""):
            return Decimal("0")
        if value < 0:
            raise forms.ValidationError("Cannot be negative.")
        return value


class PreviousAccountRegistrationForm(forms.Form):
    first_name = forms.CharField(
        max_length=30,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "First name"}),
    )
    last_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Last name"}),
    )
    mobile_number = forms.CharField(
        required=False,
        max_length=20,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "09XX-XXX-XXXX (optional)"}),
    )
    barangay = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    street_address = forms.CharField(
        max_length=500,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    municipality = forms.CharField(
        required=False,
        max_length=255,
        initial="Bayawan City",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    province = forms.CharField(
        required=False,
        max_length=255,
        initial="Negros Oriental",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    prior_desludging_m3_4y = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        initial=Decimal("0"),
        max_digits=7,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0",
                "placeholder": "0.00",
            }
        ),
        label="Past 4-year desludging volume (m³)",
    )
    def clean_mobile_number(self):
        num = (self.cleaned_data.get("mobile_number") or "").strip()
        if not num:
            return ""
        cleaned = re.sub(r"[\s\-()]", "", num)
        if not re.match(r"^(\+63|0)?9\d{9}$", cleaned):
            raise forms.ValidationError("Enter a valid Philippine mobile number (e.g. 09XX-XXX-XXXX).")
        return cleaned

    def clean_prior_desludging_m3_4y(self):
        value = self.cleaned_data.get("prior_desludging_m3_4y")
        if value in (None, ""):
            return Decimal("0")
        return value


class MembershipSearchForm(forms.Form):
    """Search form for members"""
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Search by name or username...',
        })
    )
    barangay = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Filter by barangay...',
        })
    )

