from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import Device, RepairEvaluation, TechnicalAssessment

User = get_user_model()


class RegistrationForm(UserCreationForm):
    role = forms.ChoiceField(choices=[(User.Role.DEVICE_MANAGER, "Device Manager"), (User.Role.IT_ADMIN, "Request IT Administrator access")])
    email = forms.EmailField()
    phone = forms.CharField(max_length=30, required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "phone", "role")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = self.cleaned_data["role"]
        user.email = self.cleaned_data["email"]
        user.phone = self.cleaned_data["phone"]
        user.admin_approved = user.role != User.Role.IT_ADMIN
        if commit:
            user.save()
        return user


class DeviceRegistrationForm(forms.ModelForm):
    class Meta:
        model = Device
        fields = ("asset_tag", "manufacturer", "model_name", "serial_number", "processor", "ram_gb", "storage_gb", "screen_inches", "initial_purchase_value", "year_of_purchase", "refurbished", "is_public")
        widgets = {
            "initial_purchase_value": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "year_of_purchase": forms.NumberInput(attrs={"min": "1990", "max": "2100"}),
        }

    def clean_initial_purchase_value(self):
        value = self.cleaned_data["initial_purchase_value"]
        if value <= 0:
            raise forms.ValidationError("Purchase value must be greater than zero.")
        return value

    def clean(self):
        cleaned = super().clean()
        year = cleaned.get("year_of_purchase")
        if year and year > __import__("datetime").date.today().year:
            self.add_error("year_of_purchase", "Purchase year cannot be in the future.")
        return cleaned


class TechnicalAssessmentForm(forms.ModelForm):
    class Meta:
        model = TechnicalAssessment
        fields = (*TechnicalAssessment.COMPONENTS, "notes")
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class RepairEvaluationForm(forms.ModelForm):
    class Meta:
        model = RepairEvaluation
        fields = ("result", "technician_comments")
        widgets = {"technician_comments": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("result") == RepairEvaluation.Result.FAILED and not cleaned.get("technician_comments", "").strip():
            self.add_error("technician_comments", "Technician comments are required when a device fails.")
        return cleaned


class SelfAssessmentForm(forms.Form):
    COMPONENTS = ("boot", "keyboard", "storage", "cpu", "bluetooth", "usb", "display", "battery_health", "ram", "wifi", "camera")
    PATHWAYS = (("sanitize", "Option A: Sanitization"), ("repair", "Option B: Repair"), ("both", "Option C: Both"))
    device = forms.ModelChoiceField(queryset=Device.objects.none())
    pathway = forms.ChoiceField(choices=PATHWAYS, widget=forms.RadioSelect)
    backup_confirmed = forms.BooleanField(required=True, label="I have backed up my data and understand it may be permanently erased.")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["device"].queryset = Device.objects.filter(owner=user)
        for component in self.COMPONENTS:
            self.fields[component] = forms.IntegerField(min_value=1, max_value=10, initial=7, widget=forms.NumberInput(attrs={"type": "range", "class": "condition-slider", "min": 1, "max": 10}))


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone")