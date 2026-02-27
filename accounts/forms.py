import re

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import ConsumerProfile, User


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Username"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Password"}),
    )


class ConsumerRegistrationForm(UserCreationForm):
    first_name = forms.CharField(
        max_length=30,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "First name"})
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Last name"})
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"class": "form-control", "placeholder": "Email address"}),
        error_messages={
            "invalid": "Please enter a valid email address (e.g. user@domain.com).",
            "required": "This field is required.",
        },
    )
    gender = forms.ChoiceField(
        choices=ConsumerProfile.Gender.choices,
        required=True,
        widget=forms.RadioSelect(attrs={"class": "gender-radio"}),
    )
    birthdate = forms.DateField(
        required=True,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
        error_messages={"required": "Birthdate is required."},
    )
    mobile_number = forms.CharField(
        max_length=20,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "09XX-XXX-XXXX"}),
        error_messages={"required": "Mobile number is required."},
    )
    street_address = forms.CharField(
        max_length=500,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "House No., Street, Purok/Sitio"}),
    )
    barangay = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Barangay"}),
        error_messages={"required": "Barangay is required."},
    )
    municipality = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "City / Municipality"}),
        error_messages={"required": "City / Municipality is required."},
    )
    province = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Province"}),
        error_messages={"required": "Province is required."},
    )
    gps_latitude = forms.DecimalField(
        required=False,
        widget=forms.HiddenInput(),
    )
    gps_longitude = forms.DecimalField(
        required=False,
        widget=forms.HiddenInput(),
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "password1", "password2")
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"class": "form-control", "placeholder": "Username"})
        self.fields["password1"].widget.attrs.update({"class": "form-control", "placeholder": "Password"})
        self.fields["password2"].widget.attrs.update({"class": "form-control", "placeholder": "Password confirmation"})
        self.fields["password2"].error_messages["password_mismatch"] = "Passwords do not match. Please enter the same password in both fields."
        for field_name, field in self.fields.items():
            if field_name in self.errors:
                cls = field.widget.attrs.get("class", "")
                field.widget.attrs["class"] = f"{cls} is-invalid".strip()

    def clean_mobile_number(self):
        num = self.cleaned_data.get("mobile_number", "")
        cleaned = re.sub(r"[\s\-()]", "", num)
        if not re.match(r"^(\+63|0)?9\d{9}$", cleaned):
            raise forms.ValidationError("Enter a valid Philippine mobile number (e.g. 09XX-XXX-XXXX).")
        return cleaned

    def clean(self):
        cleaned_data = super().clean()
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.CONSUMER
        user.is_active = True
        user.is_approved = True  # Consumers can be auto-approved; tweak if manual approval is needed.
        if commit:
            user.save()
            ConsumerProfile.objects.create(
                user=user,
                gender=self.cleaned_data.get("gender") or "MALE",
                birthdate=self.cleaned_data.get("birthdate"),
                mobile_number=self.cleaned_data.get("mobile_number") or "",
                street_address=self.cleaned_data.get("street_address") or "",
                barangay=self.cleaned_data.get("barangay") or "",
                municipality=self.cleaned_data.get("municipality") or "",
                province=self.cleaned_data.get("province") or "",
                gps_latitude=self.cleaned_data.get("gps_latitude"),
                gps_longitude=self.cleaned_data.get("gps_longitude"),
            )
        return user


class ProfileUpdateForm(forms.Form):
    """Allows a consumer to update their personal information."""

    profile_picture = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={"class": "form-control", "accept": "image/*"}),
    )
    first_name = forms.CharField(
        max_length=30,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    gender = forms.ChoiceField(
        choices=ConsumerProfile.Gender.choices,
        required=True,
        widget=forms.RadioSelect(attrs={"class": "gender-radio"}),
    )
    birthdate = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"class": "form-control"}),
        error_messages={"invalid": "Please enter a valid email address."},
    )
    mobile_number = forms.CharField(
        max_length=20,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "09XX-XXX-XXXX"}),
    )
    street_address = forms.CharField(
        max_length=500,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "House No., Street, Purok/Sitio"}),
    )
    barangay = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    municipality = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    province = forms.CharField(
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def clean_mobile_number(self):
        num = self.cleaned_data.get("mobile_number", "")
        cleaned = re.sub(r"[\s\-()]", "", num)
        if not re.match(r"^(\+63|0)?9\d{9}$", cleaned):
            raise forms.ValidationError("Enter a valid Philippine mobile number (e.g. 09XX-XXX-XXXX).")
        return cleaned


class StaffRegistrationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.STAFF
        user.is_active = True
        user.is_approved = False  # Requires admin approval
        if commit:
            user.save()
        return user

