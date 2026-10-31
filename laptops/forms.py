import datetime

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils import timezone

from .models import Device, OwnerSelfAssessment, RepairEvaluation, TechnicalAssessment

User = get_user_model()


class RegistrationForm(UserCreationForm):
    role = forms.ChoiceField(choices=User.ROLE_CHOICES)
    email = forms.EmailField()

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "phone_number", "role")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = self.cleaned_data["role"]
        user.admin_approval_status = "PENDING" if user.role == "IT_ADMIN" else "APPROVED"
        if commit:
            user.save()
        return user


class DeviceRegistrationForm(forms.ModelForm):
    model_name = forms.ChoiceField(choices=(), required=True)
    acquisition_month = forms.ChoiceField(
        choices=[(f"{month:02d}", f"{month:02d}") for month in range(1, 13)],
        required=True,
        widget=forms.HiddenInput(),
    )
    acquisition_year = forms.ChoiceField(choices=(), required=True, widget=forms.HiddenInput())
    acquisition_period = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Device
        fields = (
            "device_name", "make", "model_name", "serial_number", "processor",
            "ram_gb", "storage_info", "initial_value_kes", "acquisition_month",
            "acquisition_year", "acquisition_period",
        )
        widgets = {"initial_value_kes": forms.NumberInput(attrs={"step": "0.01", "min": "0"})}

    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        if owner is not None:
            self.instance.owner = owner
        make = self.data.get(self.add_prefix("make")) or self.initial.get("make") or getattr(self.instance, "make", "")
        self.fields["model_name"].choices = [("", "Choose a model series")] + list(dict(Device.MODEL_SERIES).get(make, ()))
        current_year = timezone.localdate().year
        self.fields["acquisition_year"].choices = [(str(year), str(year)) for year in range(current_year, current_year - 16, -1)]
        self.fields["acquisition_year"].widget.attrs["max_length"] = None

    def clean_device_name(self):
        name = self.cleaned_data["device_name"].strip()
        if self.owner and Device.objects.filter(owner=self.owner, device_name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("You already have a device with this nickname.")
        return name

    def clean_serial_number(self):
        serial = self.cleaned_data["serial_number"].strip()
        duplicate = Device.objects.filter(serial_number__iexact=serial).exclude(pk=self.instance.pk).exists()
        if duplicate:
            raise forms.ValidationError("This serial number is already registered in the system.")
        return serial

    def clean_initial_value_kes(self):
        value = self.cleaned_data["initial_value_kes"]
        if value <= 0:
            raise forms.ValidationError("Purchase value must be greater than zero.")
        return value

    def clean(self):
        cleaned = super().clean()
        make = cleaned.get("make")
        model = cleaned.get("model_name")
        allowed_models = dict(Device.MODEL_SERIES).get(make, ())
        if model and model not in dict(allowed_models):
            self.add_error("model_name", "Choose a model series for the selected manufacturer.")

        month = cleaned.get("acquisition_month")
        year = cleaned.get("acquisition_year")
        if month and year:
            period = f"{year}-{month}"
            period_date = datetime.date(int(year), int(month), 1)
            today = timezone.localdate()
            if period_date > today.replace(day=1):
                self.add_error("acquisition_month", "Purchase month and year cannot be in the future.")
            else:
                cleaned["acquisition_period"] = period
        return cleaned


class OwnerSelfAssessmentForm(forms.ModelForm):
    device = forms.ModelChoiceField(queryset=Device.objects.none())
    backup_confirmed = forms.BooleanField(required=True, label="I have backed up my files and understand sanitization permanently erases stored data.")

    class Meta:
        model = OwnerSelfAssessment
        fields = ("device", "screen_status", "keyboard_status", "battery_status", "chassis_status", "storage_status", "chosen_pathway", "notes")
        widgets = {"screen_status": forms.RadioSelect, "keyboard_status": forms.RadioSelect, "battery_status": forms.RadioSelect, "chassis_status": forms.RadioSelect, "storage_status": forms.RadioSelect, "chosen_pathway": forms.RadioSelect, "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["device"].queryset = Device.objects.filter(owner=user, status=Device.Status.REGISTERED)
        labels = {
            "screen_status": "Display & Screen",
            "battery_status": "Battery & Power",
            "keyboard_status": "Keyboard & Touchpad",
            "storage_status": "Storage & System Speed",
            "chassis_status": "Outer Casing / Body",
        }
        for field_name, label in labels.items():
            self.fields[field_name].label = label


class TechnicalAssessmentForm(forms.ModelForm):
    class Meta:
        model = TechnicalAssessment
        fields = ("screen_status", "keyboard_status", "battery_status", "chassis_status", "storage_status", "notes")
        widgets = {"screen_status": forms.RadioSelect, "keyboard_status": forms.RadioSelect, "battery_status": forms.RadioSelect, "chassis_status": forms.RadioSelect, "storage_status": forms.RadioSelect, "notes": forms.Textarea(attrs={"rows": 3})}


class RepairEvaluationForm(forms.ModelForm):
    comments = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    class Meta:
        model = RepairEvaluation
        fields = ("repair_result", "comments")
        widgets = {"repair_result": forms.RadioSelect}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("repair_result") == "NOT_FIXED" and not cleaned.get("comments", "").strip():
            self.add_error("comments", "Explain why this component could not be fixed.")
        return cleaned


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "phone_number")