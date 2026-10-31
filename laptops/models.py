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
        PENDING_ADMIN_ASSESMENT = "PENDING_Admin_assesment", "Pending IT Admin Assessment"
        IT_ADMIN_ASSESMENT_IN_PROGRESS = "IT_ADMIN_ASSESMENT_IN_PROGRESS", "IT Assessment In Progress"
        IT_ADMIN_ASSESMENT_COMPLETE = "IT_Admin_assesment_complete", "IT Assessment Complete"
        SCHEDULED_FOR_COLLECTION = "SCHEDULED_FOR_COLLECTION", "Scheduled for Collection"
        COLLECTED = "COLLECTED", "Collected"
        DONATED = "DONATED", "Donated"

    STATUS_CHOICES = [
        ('REGISTERED', 'Registered'),
        ('PENDING_Admin_assesment', 'Pending IT Admin Assessment'),
        ('IT_ADMIN_ASSESMENT_IN_PROGRESS', 'IT Assessment In Progress'),
        ('IT_Admin_assesment_complete', 'IT Assessment Complete'),
        ('SCHEDULED_FOR_COLLECTION', 'Scheduled for Collection'),
        ('COLLECTED', 'Collected'),
        ('SANITIZATION_FAILED', 'SANITIZATION_FAILED'),
        ('FAILED', 'FAILED'),
        ('SANITIZED', 'SANITIZED'),
        ('COMPLETED', 'Completed'),
        ('DONATED', 'Donated'),
    ]

    # Compatibility names for existing internal workflows; values are the finalized statuses.
    Status.PENDING_PROCESSING = Status.PENDING_ADMIN_ASSESMENT
    Status.IT_IN_PROGRESS = Status.IT_ADMIN_ASSESMENT_IN_PROGRESS
    Status.IT_VERIFIED = Status.IT_ADMIN_ASSESMENT_COMPLETE
    Status.SELF_ASSESSED = Status.PENDING_ADMIN_ASSESMENT
    Status.COMPLETED = "COMPLETED"
    Status.SANITIZED = "SANITIZED"
    Status.FAILED = "FAILED"
    Status.SANITIZATION_FAILED = "SANITIZATION_FAILED"
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
    purchase_date = models.DateField(default=datetime.date.today)
    acquisition_period = models.CharField(max_length=7, blank=True, default="", help_text="Purchase month and year in YYYY-MM format")
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="REGISTERED")
    failure_notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["owner", "device_name"], name="unique_owner_device_nickname")]

    @property
    def current_net_valuation(self):
        purchase_date = self.purchase_date
        if purchase_date is None and self.acquisition_period:
            try:
                year, month = (int(part) for part in self.acquisition_period.split("-", 1))
                purchase_date = datetime.date(year, month, 1)
            except (TypeError, ValueError):
                purchase_date = None
        if purchase_date is None:
            purchase_date = datetime.date.today()

        age = max(0, datetime.date.today().year - purchase_date.year)
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
        ("SANITIZATION", "Sanitization & Wiping"),
        ("REPAIR", "Repair"),
        ("BOTH", "Repair & Sanitization"),
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

    STATUS_POINT_RATIO = {
        "WORKING": 1.0,
        "PARTIALLY_WORKING": 0.5,
        "NOT_WORKING": 0.0,
        "MISSING": 0.0,
        "NOT_ASSESSED": 0.0,
    }

    def component_score(self, status_field, maximum):
        ratio = self.STATUS_POINT_RATIO.get(getattr(self, status_field), 0.0)
        return round(maximum * ratio)

    @property
    def storage_speed_score(self):
        return self.component_score("storage_status", 25)

    @property
    def display_screen_score(self):
        return self.component_score("screen_status", 25)

    @property
    def battery_power_score(self):
        return self.component_score("battery_status", 20)

    @property
    def keyboard_touchpad_score(self):
        return self.component_score("keyboard_status", 15)

    @property
    def body_chassis_score(self):
        return self.component_score("chassis_status", 15)

    # Readable aliases for templates and API consumers.
    @property
    def storage_score(self):
        return self.storage_speed_score

    @property
    def display_score(self):
        return self.display_screen_score

    @property
    def battery_score(self):
        return self.battery_power_score

    @property
    def keyboard_score(self):
        return self.keyboard_touchpad_score

    @property
    def chassis_score(self):
        return self.body_chassis_score

    @property
    def overall_health_score(self):
        return (
            self.storage_speed_score + self.display_screen_score + self.battery_power_score
            + self.keyboard_touchpad_score + self.body_chassis_score
        )

    @property
    def health_score(self):
        return self.overall_health_score

    @property
    def storage_speed_percentage(self):
        return round(self.storage_speed_score / 25 * 100)

    @property
    def display_screen_percentage(self):
        return round(self.display_screen_score / 25 * 100)

    @property
    def battery_power_percentage(self):
        return round(self.battery_power_score / 20 * 100)

    @property
    def keyboard_touchpad_percentage(self):
        return round(self.keyboard_touchpad_score / 15 * 100)

    @property
    def body_chassis_percentage(self):
        return round(self.body_chassis_score / 15 * 100)

    @property
    def overall_score(self):
        return self.overall_health_score

    @property
    def health_badge_label(self):
        if self.overall_score >= 80:
            return "Good Condition"
        if self.overall_score >= 50:
            return "Average Condition"
        return "Needs Attention / Repair"

    @property
    def health_badge_color(self):
        if self.overall_score >= 80:
            return "bg-emerald-100 text-emerald-700 border border-emerald-200"
        if self.overall_score >= 50:
            return "bg-amber-100 text-amber-700 border border-amber-200"
        return "bg-rose-100 text-rose-700 border border-rose-200"

    @property
    def color_badge(self):
        return self.health_badge_label

    @property
    def condition_label(self):
        return self.color_badge

    @property
    def badge_label(self):
        return self.color_badge

    @property
    def badge_class(self):
        if self.overall_health_score >= 80:
            return "bg-emerald-100 text-emerald-700"
        if self.overall_health_score >= 50:
            return "bg-amber-100 text-amber-700"
        return "bg-rose-100 text-rose-700"

    @property
    def badge_color(self):
        if self.overall_health_score >= 80:
            return "emerald"
        if self.overall_health_score >= 50:
            return "amber"
        return "rose"


class ITAssessmentScoringEngine:
    OPTIONS = (
        ("GOOD", "Working / Good"),
        ("SOMEWHAT", "Somewhat Working"),
        ("POOR", "Poor / Spoilt"),
        ("NOT_AVAILABLE", "Not Available"),
    )
    MULTIPLIERS = {
        "GOOD": 1.0,
        "SOMEWHAT": 0.5,
        "POOR": 0.0,
        "NOT_AVAILABLE": None,
    }
    MODULES = {
        "Motherboard": (15.0, {"q1_1": "Is the motherboard functioning properly?", "q1_2": "Are the motherboard's ports and connectors working?", "q1_3": "Is the motherboard free from physical or liquid damage?", "q1_4": "Does the motherboard operate without unexpected interruptions?"}),
        "CPU / Processor": (10.0, {"q2_1": "Is the processor functioning properly?", "q2_2": "Does the processor pass diagnostic tests?", "q2_3": "Does the processor operate within acceptable temperature limits?", "q2_4": "Does the processor execute tasks without unexpected errors?"}),
        "RAM / Memory": (10.0, {"q3_1": "Is the installed RAM functioning properly?", "q3_2": "Does the RAM pass memory diagnostic tests?", "q3_3": "Are all RAM slots functioning properly?", "q3_4": "Are the RAM modules free from physical damage?"}),
        "Storage HDD / SSD": (12.0, {"q4_1": "Is the storage drive functioning properly?", "q4_2": "Is the storage drive in good health?", "q4_3": "Can the storage drive read and write data correctly?", "q4_4": "Is the storage drive free from critical errors?", "q4_5": "Does the storage drive operate without unusual noises (HDD only)?"}),
        "Power System": (10.0, {"q5_1": "Is the battery functioning properly?", "q5_2": "Does the battery hold charge adequately?", "q5_3": "Does the battery charge correctly?", "q5_4": "Is the power adapter functioning properly?", "q5_5": "Is the battery free from physical damage or swelling?"}),
        "Cooling System": (8.0, {"q6_1": "Is the cooling fan functioning properly?", "q6_2": "Does the cooling fan operate without unusual noises?", "q6_3": "Is the cooling system free from dust and blockages?", "q6_4": "Does the cooling system regulate temperatures properly?", "q6_5": "Is the heat sink functioning and physically intact?"}),
        "GPU / Graphics Unit": (8.0, {"q7_1": "Is the GPU functioning properly?", "q7_2": "Does the GPU pass diagnostic tests?", "q7_3": "Does the GPU process and display graphics correctly?", "q7_4": "Does the GPU operate within acceptable temperature limits?", "q7_5": "Is the GPU free from driver-related errors?"}),
        "Display / Screen": (8.0, {"q8_1": "Is the display producing a clear image?", "q8_2": "Is the screen backlight functioning properly?", "q8_3": "Is the display free from dead pixels, lines and visual distortions?", "q8_4": "Is the screen free from physical damage?", "q8_5": "Does the display function properly when tested with an external monitor?"}),
        "Keyboard": (5.0, {"q9_1": "Are all keyboard keys functioning properly?", "q9_2": "Are all keyboard keys physically intact?", "q9_3": "Does the keyboard respond consistently to input?", "q9_4": "Is the keyboard backlight functioning properly?"}),
        "Touchpad": (5.0, {"q10_1": "Is the touchpad detecting finger movements correctly?", "q10_2": "Are the left and right-click functions working?", "q10_3": "Are the touchpad's multi-touch functions working?", "q10_4": "Is the touchpad physically intact?"}),
        "Network Adapter": (5.0, {"q11_1": "Is the Wi-Fi adapter functioning properly?", "q11_2": "Can the laptop connect to Wi-Fi networks successfully?", "q11_3": "Does the Wi-Fi connection remain stable?", "q11_4": "Is the Ethernet port functioning properly?", "q11_5": "Are the network drivers functioning correctly?"}),
        "Audio System": (4.0, {"q12_1": "Are the internal speakers functioning properly?", "q12_2": "Are both speakers producing clear sound?", "q12_3": "Is the microphone functioning properly?", "q12_4": "Is the headphone jack functioning properly?", "q12_5": "Are the audio drivers functioning correctly?"}),
    }
    ALL_FIELDS = tuple(field for _weight, questions in MODULES.values() for field in questions)

    @classmethod
    def multiplier_for(cls, answer):
        normalized = {
            "Working / Good": "GOOD",
            "Good / Working": "GOOD",
            "Somewhat Working": "SOMEWHAT",
            "Poor / Spoilt": "POOR",
            "Bad / Spoilt": "POOR",
            "Intermittent": "SOMEWHAT",
            "Not Tested": "SOMEWHAT",
            "Not Available": "NOT_AVAILABLE",
        }.get(answer, answer)
        return cls.MULTIPLIERS.get(normalized)


class ITAssessment(models.Model):
    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="it_assessment")
    assessed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    for _field_name in ITAssessmentScoringEngine.ALL_FIELDS:
        locals()[_field_name] = models.CharField(max_length=20, choices=ITAssessmentScoringEngine.OPTIONS, blank=True, default="")

ITAssessment.questions = ITAssessmentScoringEngine.MODULES


def _answer_values(answer):
    return ITAssessmentScoringEngine.MULTIPLIERS.get(answer)


ITAssessment.overall_score = property(lambda self: self.calculate_overall_score())
ITAssessment.health_score = ITAssessment.overall_score
ITAssessment.module_scores = property(lambda self: self.calculate_module_scores())


def _calculate_module_scores(self):
    results = {}
    available_weight = 0.0
    earned_weight = 0.0
    for module_name, (weight, questions) in ITAssessmentScoringEngine.MODULES.items():
        values = []
        for field_name in questions:
            value = getattr(self, field_name, "")
            multiplier = ITAssessmentScoringEngine.multiplier_for(value)
            if multiplier is not None:
                values.append(multiplier)
        if values:
            achieved = sum(values) / len(values)
            question_weight = weight / len(questions)
            available_weight += question_weight * len(values)
            earned_weight += question_weight * sum(values)
            results[module_name] = round(achieved * 100, 1)
        else:
            results[module_name] = None
    return results, round(earned_weight / available_weight * 100, 1) if available_weight else 0.0

ITAssessment.calculate_module_scores = lambda self: _calculate_module_scores(self)[0]
ITAssessment.calculate_overall_score = lambda self: _calculate_module_scores(self)[1]
ITAssessment.answered_count = property(lambda self: sum(bool(getattr(self, field) or getattr(self, field) == "NOT_AVAILABLE") for field in ITAssessmentScoringEngine.ALL_FIELDS))

ITAssessment.rating_label = property(lambda self: "EXCELLENT / REISSUE READY" if self.overall_score >= 85 else "GOOD / MINOR REPAIR" if self.overall_score >= 65 else "FAIR / RECOVERY & WIPING" if self.overall_score >= 45 else "CRITICAL / DECOMMISSION")
ITAssessment.rating_color = property(lambda self: "emerald" if self.overall_score >= 85 else "blue" if self.overall_score >= 65 else "amber" if self.overall_score >= 45 else "rose")
ITAssessment.rating_tailwind = property(lambda self: "bg-emerald-100 text-emerald-700 border-emerald-200" if self.overall_score >= 85 else "bg-blue-100 text-blue-700 border-blue-200" if self.overall_score >= 65 else "bg-amber-100 text-amber-700 border-amber-200" if self.overall_score >= 45 else "bg-rose-100 text-rose-700 border-rose-200")


class ITAssessmentDraft(models.Model):
    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="it_assessment_draft")
    answers = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
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