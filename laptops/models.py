import datetime
import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    ROLE_CHOICES = (
        ("IT_ADMIN", "IT Administrator"),
        ("DEVICE_MANAGER", "Device Manager"),
    )
    APPROVAL_CHOICES = (
        ("PENDING", "Pending Approval"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="DEVICE_MANAGER")
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    admin_approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default="APPROVED")

    def is_it_admin(self):
        return self.is_superuser or (self.role == "IT_ADMIN" and self.admin_approval_status == "APPROVED")

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class Device(models.Model):
    class Status(models.TextChoices):
        REGISTERED = "REGISTERED", "Registered"
        SELF_ASSESSED = "SELF_ASSESSED", "Self Assessed"
        PENDING_PROCESSING = "PENDING_PROCESSING", "Pending Processing"
        IT_VERIFIED = "IT_VERIFIED", "IT Admin Verified"
        SANITIZED = "SANITIZED", "Sanitization Completed"
        FAILED = "FAILED", "Sanitization Failed"
        COMPLETED = "COMPLETED", "Completed"
        DONATED = "DONATED", "Donated for Parts"

    STATUS_CHOICES = Status.choices
    MANUFACTURER_CHOICES = (
        ("Dell", "Dell"),
        ("HP", "HP (Hewlett-Packard)"),
        ("Lenovo", "Lenovo"),
        ("Acer", "Acer"),
        ("ASUS", "ASUS"),
        ("Apple", "Apple"),
        ("Toshiba", "Toshiba"),
    )
    PROCESSOR_CHOICES = (
        ("Intel", "Intel"),
        ("AMD", "AMD"),
        ("Apple", "Apple"),
        ("Qualcomm", "Qualcomm"),
        ("MediaTek", "MediaTek"),
    )
    MODEL_SERIES = (
        ("Dell", (("Latitude", "Latitude"), ("Inspiron", "Inspiron"), ("XPS", "XPS"), ("Precision", "Precision"), ("Vostro", "Vostro"))),
        ("HP", (("EliteBook", "EliteBook"), ("ProBook", "ProBook"), ("Pavilion", "Pavilion"), ("ZBook", "ZBook"), ("Envy", "Envy"))),
        ("Lenovo", (("ThinkPad", "ThinkPad"), ("ThinkBook", "ThinkBook"), ("IdeaPad", "IdeaPad"), ("Yoga", "Yoga"), ("Legion", "Legion"))),
        ("Acer", (("Aspire", "Aspire"), ("Swift", "Swift"), ("TravelMate", "TravelMate"), ("Predator", "Predator"))),
        ("ASUS", (("Zenbook", "Zenbook"), ("Vivobook", "Vivobook"), ("ExpertBook", "ExpertBook"), ("ROG", "ROG"), ("TUF", "TUF"))),
        ("Apple", (("MacBook Air", "MacBook Air"), ("MacBook Pro", "MacBook Pro"))),
        ("Toshiba", (("Dynabook Satellite", "Dynabook Satellite"), ("Portégé", "Portégé"), ("Tecra", "Tecra"))),
    )

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    device_name = models.CharField(max_length=100, help_text="Nickname for your device")
    make = models.CharField(max_length=50, choices=MANUFACTURER_CHOICES)
    model_name = models.CharField(max_length=100)
    serial_number = models.CharField(max_length=100, unique=True)
    processor = models.CharField(max_length=50, choices=PROCESSOR_CHOICES)
    ram_gb = models.IntegerField(default=8)
    storage_info = models.CharField(max_length=100, default="256GB SSD")
    initial_value_kes = models.DecimalField(max_digits=10, decimal_places=2, default=50000.00)
    purchase_date = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="REGISTERED")
    failure_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "device_name"], name="unique_owner_device_nickname")]

    @property
    def current_net_valuation(self):
        age = max(0, datetime.date.today().year - self.purchase_date.year)
        depreciated_baseline = float(self.initial_value_kes) * ((1 - 0.15) ** age)
        return round(max(5000.0, depreciated_baseline), 2)

    @property
    def cashback_incentive(self):
        return round(self.current_net_valuation * 0.03, 2)

    def __str__(self):
        return f"{self.device_name} - {self.make} {self.model_name} ({self.serial_number})"


class OwnerSelfAssessment(models.Model):
    COMPONENT_STATUS_CHOICES = (
        ("WORKING", "Working"),
        ("PARTIALLY_WORKING", "Partially Working"),
        ("NOT_WORKING", "Not Working"),
        ("MISSING", "Missing"),
        ("NOT_ASSESSED", "Not Assessed"),
    )
    PATHWAY_CHOICES = (
        ("SANITIZATION", "Sanitization"),
        ("REPAIR", "Repair"),
        ("BOTH", "Repair and sanitization"),
    )

    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="self_assessment")
    screen_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    keyboard_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    battery_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    chassis_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    storage_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    chosen_pathway = models.CharField(max_length=50, choices=PATHWAY_CHOICES, default="SANITIZATION")
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class TechnicalAssessment(models.Model):
    COMPONENT_STATUS_CHOICES = OwnerSelfAssessment.COMPONENT_STATUS_CHOICES
    COMPONENTS = ("screen_status", "keyboard_status", "battery_status", "chassis_status", "storage_status")

    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="technical_assessment")
    assessed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    screen_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    keyboard_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    battery_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    chassis_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    storage_status = models.CharField(max_length=20, choices=COMPONENT_STATUS_CHOICES, default="WORKING")
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class RepairEvaluation(models.Model):
    REPAIR_RESULT_CHOICES = (("FIXED", "Fixed"), ("NOT_FIXED", "Not Fixed"))

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="repairs")
    technician = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    component_name = models.CharField(max_length=50, default="General Hardware")
    repair_result = models.CharField(max_length=20, choices=REPAIR_RESULT_CHOICES)
    comments = models.TextField(help_text="Mandatory explanation if component is Not Fixed")
    created_at = models.DateTimeField(auto_now_add=True)


class SanitizationJob(models.Model):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="sanitization_jobs")
    status = models.CharField(max_length=30, default="PENDING")
    started_at = models.DateTimeField(auto_now_add=True)


class SanitizationCertificate(models.Model):
    cert_code = models.CharField(max_length=50, unique=True, default=uuid.uuid4)
    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="certificate")
    verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    wipe_method = models.CharField(max_length=100, default="DoD 5220.22-M 3-Pass Overwrite")
    crypto_hash = models.CharField(max_length=64, default="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    issued_at = models.DateTimeField(auto_now_add=True)


class CollectionSlot(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="collection_slots")
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="collection_slots")
    scheduled_for = models.DateTimeField()
    location = models.CharField(max_length=160, default="Strathmore University")
    approved = models.BooleanField(default=False)
    collected = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class DonationLedger(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        PAID = "PAID", "Paid"
        REJECTED = "REJECTED", "Rejected"

    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="donation_ledger")
    donated_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="donation_rewards")
    donation_value_kes = models.DecimalField(max_digits=10, decimal_places=2)
    cashback_kes = models.DecimalField(max_digits=10, decimal_places=2)
    payout_status = models.CharField(max_length=20, choices=Status.choices, default=Status.PAID)
    transaction_ref = models.CharField(max_length=50, default="MPESA-MOCK-TXN")
    donated_at = models.DateTimeField(auto_now_add=True)


class AuditLog(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="audit_logs")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    action_stage = models.CharField(max_length=50, default="REGISTERED")
    description = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)