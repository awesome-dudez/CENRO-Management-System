from django import forms
from django.conf import settings
from decimal import Decimal
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
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.01',
            'placeholder': 'Cubic meters',
        })
    )
    distance_km = forms.DecimalField(
        required=False,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.01',
            'placeholder': 'Distance (km) - if outside Bayawan',
        })
    )
    meals_transport = forms.DecimalField(
        required=False,
        initial=Decimal('0'),
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'step': '0.01',
            'placeholder': 'Meals & Transport allowance',
        })
    )


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

