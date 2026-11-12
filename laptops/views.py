from decimal import Decimal
from datetime import datetime, date
from io import BytesIO
import hashlib
import uuid
import qrcode
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db.models import F, Max, Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.utils.text import slugify

from .forms import DeviceRegistrationForm, OwnerSelfAssessmentForm, ProfileForm, RegistrationForm, RepairEvaluationForm, TechnicalAssessmentForm
from .models import AuditLog, CollectionSlot, Device, DonationLedger, ITAssessment, ITAssessmentDraft, ITAssessmentScoringEngine, OwnerSelfAssessment, RepairEvaluation, RepairExecutionDraft, SanitizationCertificate, SanitizationJob, TechnicalAssessment
from .utils import calculate_device_valuation, format_kes, SUBSYSTEM_WEIGHTS, _score_component

User = get_user_model()


def _advance_both_stage(device, completed_stage):
    """Move a BOTH pathway device through sanitization and repair, then to disposition."""
    assessment = getattr(device, "self_assessment", None)
    if not assessment or assessment.chosen_pathway != "BOTH":
        return False

    update_fields = []
    if completed_stage == "sanitization":
        device.sanitization_tag = "complete"
        update_fields.append("sanitization_tag")
        if device.repair_tag in {"complete", "completed"}:
            device.status = Device.Status.DISPOSITION_PENDING
            update_fields.append("status")
        else:
            device.repair_tag = "pending"
            device.status = Device.Status.REPAIR_AND_SANITIZATION_PENDING
            update_fields.extend(["repair_tag", "status"])
    elif completed_stage == "repair":
        device.repair_tag = "complete"
        update_fields.append("repair_tag")
        if device.sanitization_tag in {"complete", "completed"}:
            device.status = Device.Status.DISPOSITION_PENDING
            update_fields.append("status")
        else:
            device.sanitization_tag = "pending"
            device.status = Device.Status.REPAIR_AND_SANITIZATION_PENDING
            update_fields.extend(["sanitization_tag", "status"])

    if update_fields:
        update_fields.append("updated_at")
        device.save(update_fields=list(dict.fromkeys(update_fields)))
    return True


def _service_pathway_status(device):
    assessment = getattr(device, "self_assessment", None)
    if assessment is None:
        return {"label": "Not selected", "completed": False, "components": []}

    pathway = assessment.chosen_pathway
    components = []

    if pathway == "SANITIZATION":
        completed = (
            device.sanitization_tag in {"complete", "completed"}
            or getattr(device, "certificate", None) is not None
        )
        components.append(("Sanitization", completed))
    elif pathway == "REPAIR":
        completed = device.repair_tag in {"complete", "completed"}
        components.append(("Repair", completed))
    else:
        sanitization_completed = (
            device.sanitization_tag in {"complete", "completed"}
            or getattr(device, "certificate", None) is not None
        )
        repair_completed = device.repair_tag in {"complete", "completed"}
        components.extend([
            ("Repair", repair_completed),
            ("Sanitization", sanitization_completed),
        ])
        completed = all(done for _label, done in components)

    return {
        "label": assessment.get_chosen_pathway_display(),
        "completed": completed if pathway != "BOTH" else completed,
        "components": components,
    }


def it_admin_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_it_admin():
            raise PermissionDenied("IT Administrator access is required.")
        return view_func(request, *args, **kwargs)
    return wrapped


def record_audit(device, actor, action, details=""):
    return AuditLog.objects.create(device=device, actor=actor, action_stage=action, description=details)


def _audit_logs_prefetch():
    return Prefetch("audit_logs", queryset=AuditLog.objects.order_by("-timestamp", "-pk"), to_attr="ordered_audit_logs")


def _searched_devices(queryset, query):
    if query:
        queryset = queryset.filter(Q(device_name__icontains=query) | Q(make__icontains=query) | Q(model_name__icontains=query) | Q(serial_number__icontains=query) | Q(owner__username__icontains=query))
    return queryset


def _effective_component_condition(device, component):
    component_name = component.capitalize()
    repair_outcomes = sorted(
        device.repairs.all(),
        key=lambda repair: repair.pk,
    ) if hasattr(device, "repairs") else []
    matching_outcomes = [
        repair.repair_result
        for repair in repair_outcomes
        if repair.component_name.lower().strip() == component_name.lower()
        or component_name.lower() in repair.component_name.lower().strip()
    ]
    if matching_outcomes:
        return "WORKING" if matching_outcomes[-1] == "FIXED" else "NOT_WORKING"

    assessment = getattr(device, "technical_assessment", None)
    if assessment is None:
        assessment = getattr(device, "self_assessment", None)
    if assessment is None:
        return "NOT_ASSESSED"
    return getattr(assessment, f"{component}_status", "NOT_ASSESSED")


def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if request.user.is_it_admin():
        return redirect("overview")
    query = request.GET.get("q", "").strip()
    devices = _searched_devices(Device.objects.filter(owner=request.user).select_related("owner", "self_assessment", "technical_assessment").order_by("-created_at", "-pk"), query)
    public_inventory = _searched_devices(
        Device.objects.filter(is_listed=True)
        .exclude(owner=request.user)
        .exclude(status__in=[Device.Status.FAILED, Device.Status.SANITIZATION_FAILED, Device.Status.DONATED])
        .select_related("owner", "self_assessment", "technical_assessment", "it_assessment")
        .prefetch_related("repairs__technician")
        .order_by("-created_at", "-pk"),
        query,
    )
    public_inventory = list(public_inventory)
    for device in public_inventory:
        device.market_valuation = calculate_device_valuation(device)
        assessment = getattr(device, "it_assessment", None)
        device.market_score = assessment.overall_score if assessment else 0
        device.market_rating = assessment.rating_label if assessment else "NOT ASSESSED"
        device.market_repairs = list(device.repairs.filter(repair_result="FIXED").select_related("technician"))
    reward_total = request.user.donation_rewards.filter(
        payout_status__in=[DonationLedger.Status.APPROVED, DonationLedger.Status.PAID]
    ).aggregate(total=Sum("cashback_kes"))["total"] or Decimal("0.00")
    recovered = DonationLedger.objects.filter(payout_status=DonationLedger.Status.PAID).aggregate(total=Sum("donation_value_kes"))["total"] or Decimal("0.00")
    return render(request, "laptops/dashboard.html", {"devices": devices, "inventory": public_inventory, "asset_count": devices.count(), "reward_total": reward_total, "recovered_total": recovered})


@it_admin_required
def overview(request):
    if request.method == "POST" and request.POST.get("action") == "mark_disbursement_paid":
        ledger = get_object_or_404(
            DonationLedger.objects.select_related("device"),
            pk=request.POST.get("entry"),
            payout_status__in=[DonationLedger.Status.PENDING, DonationLedger.Status.APPROVED],
        )
        ledger.payout_status = DonationLedger.Status.PAID
        ledger.save(update_fields=["payout_status"])
        record_audit(
            ledger.device,
            request.user,
            "Disbursement paid",
            f"Cashback payout marked paid: {format_kes(ledger.cashback_kes)}.",
        )
        messages.success(request, "Disbursement marked paid.")
        return redirect(f"{request.path}?status=awaiting_disbursement")

    base = _searched_devices(
        Device.objects.select_related(
            "owner",
            "self_assessment",
            "technical_assessment",
            "it_assessment",
            "certificate",
            "donation_ledger",
        ).prefetch_related(
            _audit_logs_prefetch(),
            Prefetch(
                "repairs",
                queryset=RepairEvaluation.objects.select_related("technician").order_by("-created_at", "-pk"),
            ),
        ),
        request.GET.get("q", "").strip(),
    )
    status_filter = request.GET.get("status", "")
    if status_filter:
        if status_filter == "awaiting_sanitization":
            base = base.filter(status__in=[
                Device.Status.SANITIZATION_PROCESSING,
                Device.Status.REPAIR_AND_SANITIZATION_PENDING,
            ])
        elif status_filter == "awaiting_repair":
            base = base.filter(status__in=[Device.Status.REPAIR_PROCESSING, Device.Status.REPAIR_AND_SANITIZATION_PENDING])
        elif status_filter == "awaiting_disbursement":
            base = base.filter(donation_ledger__payout_status__in=[
                DonationLedger.Status.PENDING,
                DonationLedger.Status.APPROVED,
            ])
        elif status_filter == "awaiting_disposition":
            base = base.filter(status__in=[
                Device.Status.IT_ADMIN_ASSESMENT_COMPLETE,
                Device.Status.SCHEDULED_FOR_COLLECTION,
            ])
        else:
            base = base.filter(status=status_filter)
    base = base.annotate(
        latest_activity=Coalesce(Max("audit_logs__timestamp"), F("created_at"))
    ).order_by("-latest_activity", "-created_at", "-pk")
    devices = list(base)
    for device in devices:
        device.next_action_status = device.get_status_display()
        device.next_action_url = ""
        device.next_action_label = ""

        donation_ledger = getattr(device, "donation_ledger", None)
        if donation_ledger and donation_ledger.payout_status in [
            DonationLedger.Status.PENDING,
            DonationLedger.Status.APPROVED,
        ]:
            device.next_action_status = "Pending disbursement approval"
            device.next_action_url = reverse("disposition")
            device.next_action_label = "Go to Disbursement"
        elif device.status == Device.Status.REGISTERED:
            device.next_action_status = "Pending owner's feedback"
        elif device.status in [
            Device.Status.PENDING_ADMIN_ASSESMENT,
            Device.Status.IT_ADMIN_ASSESMENT_IN_PROGRESS,
        ]:
            device.next_action_status = "Pending IT assessment"
            device.next_action_url = reverse("it_assessment_detail", args=[device.pk])
            device.next_action_label = "Go to IT Assessment"
        elif device.status == Device.Status.REPAIR_AND_SANITIZATION_PENDING and device.repair_tag == "pending":
            device.next_action_url = reverse("repair_execution", args=[device.pk])
            device.next_action_label = "Go to Repair"
        elif device.status == Device.Status.REPAIR_PROCESSING or device.repair_tag == "pending":
            device.next_action_status = "Repair pending"
            device.next_action_url = reverse("repair_execution", args=[device.pk])
            device.next_action_label = "Go to Repair"
        elif device.status == Device.Status.SANITIZATION_PROCESSING or (
            device.sanitization_tag == "pending"
            and device.status not in [Device.Status.DONATED, Device.Status.COLLECTED]
        ):
            device.next_action_status = "Sanitization pending"
            device.next_action_url = reverse("sanitization_execution", args=[device.pk])
            device.next_action_label = "Go to Sanitization"
        elif device.status == Device.Status.SCHEDULED_FOR_COLLECTION:
            device.next_action_status = "Pending collection approval"
            device.next_action_url = reverse("disposition")
            device.next_action_label = "Go to Disposition"
        elif device.status == Device.Status.FAILED:
            device.next_action_status = "Repair failed"
            device.next_action_url = reverse("repairs")
            device.next_action_label = "Go to Repair Queue"
        elif device.status == Device.Status.IT_ADMIN_ASSESMENT_COMPLETE:
            device.next_action_status = "Pending owner's feedback"

    context = {
        "devices": devices,
        "status_filter": status_filter,
        "total_assets": Device.objects.count(),
        "awaiting_assessment": Device.objects.filter(status=Device.Status.PENDING_ADMIN_ASSESMENT).count(),
        "awaiting_sanitization": Device.objects.filter(status__in=[
            Device.Status.SANITIZATION_PROCESSING,
            Device.Status.REPAIR_AND_SANITIZATION_PENDING,
        ]).count(),
        "awaiting_repair": Device.objects.filter(status__in=[
            Device.Status.REPAIR_PROCESSING,
            Device.Status.REPAIR_AND_SANITIZATION_PENDING,
        ]).count(),
        "awaiting_disbursement": DonationLedger.objects.filter(
            payout_status__in=[DonationLedger.Status.PENDING, DonationLedger.Status.APPROVED]
        ).values("device_id").distinct().count(),
        "awaiting_disposition": Device.objects.filter(status__in=[
            Device.Status.IT_ADMIN_ASSESMENT_COMPLETE,
            Device.Status.SCHEDULED_FOR_COLLECTION,
        ]).count(),
        "sanitization_completed": Device.objects.filter(status=Device.Status.SANITIZED).count(),
        "failed_sanitizations": SanitizationJob.objects.filter(status=SanitizationJob.FAIL).values("device_id").distinct().count(),
        "ewaste_recovered": DonationLedger.objects.filter(payout_status=DonationLedger.Status.PAID).aggregate(total=Sum("donation_value_kes"))["total"] or Decimal("0.00"),
        "status_choices": Device.STATUS_CHOICES,
    }
    return render(request, "laptops/overview.html", context)


@it_admin_required
def device_inventory(request):
    devices = _searched_devices(
        Device.objects.select_related(
            "owner",
            "self_assessment",
            "technical_assessment",
            "it_assessment",
            "certificate",
            "donation_ledger",
        ).prefetch_related(_audit_logs_prefetch(), "repairs", "sanitization_jobs"),
        request.GET.get("q", "").strip(),
    ).order_by("-created_at", "-pk")
    component = request.GET.get("component", "")
    condition = request.GET.get("condition", "")
    assessment_component = {"screen": "screen_status", "keyboard": "keyboard_status", "battery": "battery_status", "chassis": "chassis_status", "storage": "storage_status"}.get(component)
    conditions = TechnicalAssessment.COMPONENT_STATUS_CHOICES[:3]
    condition_values = {value for value, _ in conditions}
    if assessment_component in TechnicalAssessment.COMPONENTS and condition in condition_values:
        devices = [
            device for device in devices
            if _effective_component_condition(device, component) == condition
        ]
    components = [("screen", "Screen"), ("keyboard", "Keyboard"), ("battery", "Battery"), ("chassis", "Chassis"), ("storage", "Storage")]
    return render(request, "laptops/devices.html", {"devices": devices, "components": components, "conditions": conditions})


@login_required
def register_device(request):
    current_year = timezone.localdate().year
    form = DeviceRegistrationForm(request.POST or None, owner=request.user)
    if request.method == "POST" and form.is_valid():
        acquisition_month = form.cleaned_data["acquisition_month"]
        acquisition_year = form.cleaned_data["acquisition_year"]
        acquisition_period = f"{acquisition_year}-{acquisition_month}"

        device = form.save(commit=False)
        device.owner = request.user
        device.status = Device.Status.REGISTERED
        device.acquisition_period = acquisition_period
        device.purchase_date = date(int(acquisition_year), int(acquisition_month), 1)
        device.save()
        record_audit(device, request.user, "Device registered", f"{device.device_name} added to the recovery system.")
        messages.success(request, "Device registered successfully.")
        if request.user.is_it_admin():
            return redirect("devices")
        return redirect("my_devices")
    return render(request, "laptops/device_register.html", {
        "form": form,
        "is_admin": request.user.is_it_admin(),
        "model_series": dict(Device.MODEL_SERIES),
        "year_range": range(current_year, current_year - 15, -1),
        "current_year": current_year,
    })


@login_required
def my_devices(request):
    devices = _searched_devices(Device.objects.filter(owner=request.user).select_related(
        "owner",
        "self_assessment",
        "certificate",
    ).prefetch_related(_audit_logs_prefetch()).order_by("-created_at", "-pk"), request.GET.get("q", "").strip())
    unlisted_devices = []
    listed_devices = []
    for device in devices:
        assessment = getattr(device, "self_assessment", None)
        pathway = assessment.chosen_pathway if assessment else ""
        sanitization_complete = (
            device.sanitization_tag in {"complete", "completed"}
            or getattr(device, "certificate", None) is not None
        )
        repair_complete = device.repair_tag in {"complete", "completed"}

        device.next_step_url = ""
        device.next_step_label = ""
        if device.status == Device.Status.REGISTERED:
            device.processing_stage = "Owner self-assessment required"
            device.next_step_url = reverse("self_assessment_device", args=[device.pk])
            device.next_step_label = "Complete self-assessment"
        elif device.status == Device.Status.DISPOSITION_PENDING:
            device.processing_stage = "Processing complete — disposition decision required"
            device.next_step_url = reverse("disposition")
            device.next_step_label = "Choose next step"
        elif device.status in {Device.Status.SANITIZATION_FAILED, Device.Status.FAILED}:
            device.processing_stage = device.get_status_display()
        elif pathway == "BOTH" and sanitization_complete and not repair_complete:
            device.processing_stage = "Sanitization complete — awaiting repair"
        elif pathway == "BOTH" and repair_complete and not sanitization_complete:
            device.processing_stage = "Repair complete — awaiting sanitization"
        elif device.status == Device.Status.PENDING_ADMIN_ASSESMENT:
            device.processing_stage = "Awaiting IT assessment"
        elif device.status == Device.Status.IT_ADMIN_ASSESMENT_IN_PROGRESS:
            device.processing_stage = "IT assessment in progress"
        elif device.status == Device.Status.IT_ADMIN_ASSESMENT_COMPLETE:
            device.processing_stage = "IT assessment complete — awaiting service"
        elif device.status == Device.Status.SANITIZATION_PROCESSING:
            device.processing_stage = "Sanitization in progress"
        elif device.status == Device.Status.REPAIR_PROCESSING:
            device.processing_stage = "Repair in progress"
        elif device.status == Device.Status.REPAIR_AND_SANITIZATION_PENDING:
            device.processing_stage = "Repair and sanitization pending"
        elif device.status == Device.Status.SCHEDULED_FOR_COLLECTION:
            device.processing_stage = "Collection scheduled"
        elif device.status == Device.Status.COLLECTED:
            device.processing_stage = "Device collected"
        elif device.status == Device.Status.DONATED:
            device.processing_stage = "Device donated"
        elif device.status in {Device.Status.SANITIZED, Device.Status.COMPLETED} or sanitization_complete or repair_complete:
            device.processing_stage = "Processing complete"
        else:
            device.processing_stage = device.get_status_display()

        if device.is_listed:
            listed_devices.append(device)
        else:
            unlisted_devices.append(device)

    return render(request, "laptops/my_devices.html", {
        "devices": unlisted_devices,
        "listed_devices": listed_devices,
    })


@login_required
@require_POST
def remove_device_listing(request, device_id):
    device = get_object_or_404(Device, pk=device_id, owner=request.user, is_listed=True)
    device.is_listed = False
    device.save(update_fields=["is_listed", "updated_at"])
    record_audit(device, request.user, "Marketplace listing removed", "Device removed from the marketplace.")
    messages.success(request, f"{device.device_name} has been removed from the marketplace.")
    return redirect("my_devices")


@login_required
def device_detail(request, device_id):
    device = get_object_or_404(
        Device.objects.select_related(
            "owner",
            "self_assessment",
            "technical_assessment",
            "it_assessment",
            "certificate",
            "donation_ledger",
        ).prefetch_related(
            "repairs",
            "sanitization_jobs",
            "collection_slots",
            _audit_logs_prefetch(),
        ),
        pk=device_id,
    )
    if device.owner != request.user and not request.user.is_it_admin():
        raise PermissionDenied("You do not have access to this device.")
    valuation = calculate_device_valuation(device)
    repairs = list(device.repairs.all().select_related("technician").order_by("-created_at", "-pk"))
    fixed_repairs = [repair for repair in repairs if repair.repair_result == "FIXED"]
    failed_repairs = [repair for repair in repairs if repair.repair_result == "NOT_FIXED"]
    pathway = getattr(getattr(device, "self_assessment", None), "chosen_pathway", "")
    pathway_completed = (
        (
            pathway == "SANITIZATION"
            and (device.sanitization_tag in {"complete", "completed"} or hasattr(device, "certificate"))
        )
        or (pathway == "REPAIR" and device.repair_tag in {"complete", "completed"})
        or (
            pathway == "BOTH"
            and (device.sanitization_tag in {"complete", "completed"} or hasattr(device, "certificate"))
            and device.repair_tag in {"complete", "completed"}
        )
    )
    pathway_status = _service_pathway_status(device)
    context = {
        "device": device,
        "valuation": valuation,
        "repairs": repairs,
        "fixed_repairs": fixed_repairs,
        "failed_repairs": failed_repairs,
        "pathway_completed": pathway_completed,
        "pathway_status": pathway_status,
        "audit_logs": list(device.ordered_audit_logs),
        "sanitization_jobs": list(device.sanitization_jobs.all().order_by("-started_at", "-pk")),
        "collection_slots": list(device.collection_slots.all().order_by("-created_at", "-pk")),
    }
    return render(request, "laptops/device_detail.html", context)


@it_admin_required
def assessments(request):
    query = request.GET.get("q", "").strip()
    active_devices = _searched_devices(Device.objects.filter(
        status__in=[Device.Status.PENDING_PROCESSING, Device.Status.IT_IN_PROGRESS]
    ).select_related("owner").order_by("-created_at", "-pk"), query)
    devices = active_devices

    portal_queryset = _searched_devices(Device.objects.exclude(
        status=Device.Status.REGISTERED
    ).select_related(
        "owner", "self_assessment", "it_assessment", "it_assessment_draft"
    ), query)

    portal_devices = []
    for device in portal_queryset.order_by("-created_at", "-pk"):
        assessment = getattr(device, "it_assessment", None)
        if device.status in [
            Device.Status.IT_VERIFIED,
            Device.Status.SANITIZED,
            Device.Status.COMPLETED,
            Device.Status.DONATED,
            Device.Status.SCHEDULED_FOR_COLLECTION,
            Device.Status.COLLECTED,
        ] or assessment:
            category = "completed"
        elif device.status == Device.Status.IT_IN_PROGRESS:
            category = "incomplete"
        else:
            category = "pending"
        portal_devices.append({
            "device": device,
            "category": category,
            "assessment": assessment,
        })

    pending_queue = []
    pending_queryset = _searched_devices(
        Device.objects.filter(status=Device.Status.IT_IN_PROGRESS).select_related("owner", "it_assessment_draft"),
        query,
    ).order_by("-created_at", "-pk")
    for device in pending_queryset:
        draft = getattr(device, "it_assessment_draft", None)
        answers = draft.answers if draft and isinstance(draft.answers, dict) else {}
        answered = sum(1 for field in ITAssessmentScoringEngine.ALL_FIELDS if field in answers and answers[field])
        progress = round(min(answered, ITAssessmentScoringEngine.QUESTION_COUNT) / ITAssessmentScoringEngine.QUESTION_COUNT * 100)
        pending_queue.append({"device": device, "progress": progress})

    counts = {
        "all": len(portal_devices),
        "pending": sum(1 for item in portal_devices if item["category"] == "pending"),
        "incomplete": sum(1 for item in portal_devices if item["category"] == "incomplete"),
        "completed": sum(1 for item in portal_devices if item["category"] == "completed"),
    }
    requested_filter = request.GET.get("filter", "all")
    initial_filter = requested_filter if requested_filter in {"all", "pending", "incomplete", "completed"} else "all"

    selected_id = request.POST.get("device") or request.GET.get("device")
    selected = None
    if selected_id:
        selected = get_object_or_404(Device.objects.select_related("owner"), pk=selected_id)
        if selected.status not in [Device.Status.PENDING_PROCESSING, Device.Status.IT_IN_PROGRESS]:
            messages.warning(request, "Only pending or in-progress devices can be assessed.")
            return redirect("assessments")
    else:
        selected = devices.first()

    existing_assessment = TechnicalAssessment.objects.filter(device=selected).first() if selected else None
    form = TechnicalAssessmentForm(request.POST or None, instance=existing_assessment)
    if request.method == "POST" and selected and form.is_valid():
        assessment = form.save(commit=False)
        assessment.device = selected
        assessment.assessed_by = request.user
        assessment.save()
        selected.status = Device.Status.PENDING_PROCESSING
        selected.save(update_fields=["status", "updated_at"])
        record_audit(selected, request.user, "Technical assessment completed", "Component condition assessment recorded.")
        messages.success(request, "Assessment saved.")
        return redirect("assessments")

    return render(request, "laptops/it_assessments_portal.html", {
        "devices": devices,
        "active_devices": devices,
        "selected": selected,
        "form": form,
        "portal_devices": portal_devices,
        "pending_queue": pending_queue,
        "counts": counts,
        "initial_filter": initial_filter,
        "latest_assessments": TechnicalAssessment.objects.select_related("device", "assessed_by").order_by("-created_at")[:15],
    })


@it_admin_required
def assessment_report(request, device_id):
    device = get_object_or_404(
        Device.objects.select_related("owner", "self_assessment", "it_assessment"),
        pk=device_id,
    )
    assessment = getattr(device, "it_assessment", None)
    if not assessment:
        messages.warning(request, "This device does not have a completed IT assessment report.")
        return redirect("it_assessments_portal")
    repair_statuses = list(device.repairs.order_by("created_at", "pk").values("component_name", "repair_result"))
    try:
        repair_draft = device.repair_execution_draft
    except RepairExecutionDraft.DoesNotExist:
        repair_draft = None
    if repair_draft:
        saved_components = {item["component_name"] for item in repair_statuses}
        repair_statuses.extend(
            {"component_name": name, "repair_result": result}
            for name, result in (repair_draft.answers or {}).items()
            if result in ("FIXED", "NOT_FIXED") and name not in saved_components
        )
    return render(request, "laptops/assessment_report.html", {
        "device": device,
        "assessment": assessment,
        "repair_statuses": repair_statuses,
        "fixed_repairs": [item for item in repair_statuses if item["repair_result"] == "FIXED"],
        "not_fixed_repairs": [item for item in repair_statuses if item["repair_result"] == "NOT_FIXED"],
    })


@it_admin_required
def it_assessment(request, device_id=None):
    active_devices = Device.objects.filter(status__in=[Device.Status.PENDING_ADMIN_ASSESMENT, Device.Status.IT_ADMIN_ASSESMENT_IN_PROGRESS]).select_related("owner", "it_assessment_draft")
    if device_id:
        device = Device.objects.select_related("owner", "it_assessment_draft", "it_assessment").filter(pk=device_id).first()
        if device is None:
            messages.warning(request, "That device no longer exists or the link is outdated.")
            return redirect("it_assessments_portal")
        if device.status not in [Device.Status.PENDING_ADMIN_ASSESMENT, Device.Status.IT_ADMIN_ASSESMENT_IN_PROGRESS, Device.Status.IT_ADMIN_ASSESMENT_COMPLETE]:
            messages.warning(request, "Only pending, in-progress, or completed devices can be viewed here.")
            return redirect("it_assessments_portal")
    else:
        device = active_devices.first()
    if not device:
        return render(request, "laptops/it_assessment.html", {"device": None, "modules": [], "active_devices": active_devices, "pending_queue": [], "options": ITAssessmentScoringEngine.OPTIONS})
    assessment = getattr(device, "it_assessment", None)

    if device.status == Device.Status.PENDING_ADMIN_ASSESMENT:
        device.status = Device.Status.IT_ADMIN_ASSESMENT_IN_PROGRESS
        device.save(update_fields=["status", "updated_at"])

    pending_queue = []
    for queue_device in active_devices:
        queue_draft = getattr(queue_device, "it_assessment_draft", None)
        answers = queue_draft.answers if queue_draft and isinstance(queue_draft.answers, dict) else {}
        answer_count = sum(1 for field in ITAssessmentScoringEngine.ALL_FIELDS if field in answers and answers[field])
        progress = round(min(answer_count, ITAssessmentScoringEngine.QUESTION_COUNT) / ITAssessmentScoringEngine.QUESTION_COUNT * 100)
        pending_queue.append({"device": queue_device, "progress": progress})
    draft = getattr(device, "it_assessment_draft", None)
    legacy_answer_map = {
        "GOOD": "GOOD", "Good / Working": "GOOD", "Working / Good": "GOOD",
        "SOMEWHAT": "SOMEWHAT", "Needs Attention": "SOMEWHAT", "Intermittent": "SOMEWHAT", "Not Tested": "SOMEWHAT",
        "POOR": "POOR", "Bad / Spoilt": "POOR", "Poor / Spoilt": "POOR",
        "NOT_AVAILABLE": "NOT_AVAILABLE", "Not Available": "NOT_AVAILABLE",
    }
    initial_answers = draft.answers if draft else {}
    initial_answers = {field: legacy_answer_map.get(value, value) for field, value in initial_answers.items()}
    if assessment:
        initial_answers = {field: getattr(assessment, field, "") for field in ITAssessmentScoringEngine.ALL_FIELDS if getattr(assessment, field, "")}
        if draft and draft.answers:
            initial_answers.update(draft.answers)

    score_assessment = ITAssessment(device=device)
    for field_name, answer in initial_answers.items():
        if hasattr(score_assessment, field_name):
            setattr(score_assessment, field_name, answer)
    verified_score = score_assessment.overall_score

    if request.method == "POST":
        answer_map = {
            "GOOD": "GOOD", "Good / Working": "GOOD", "Working / Good": "GOOD",
            "SOMEWHAT": "SOMEWHAT", "Needs Attention": "SOMEWHAT", "Somewhat Working": "SOMEWHAT", "Intermittent": "SOMEWHAT", "Not Tested": "SOMEWHAT",
            "POOR": "POOR", "Bad / Spoilt": "POOR", "Poor / Spoilt": "POOR",
            "NOT_AVAILABLE": "NOT_AVAILABLE", "Not Available": "NOT_AVAILABLE",
        }
        answers = {field: answer_map.get(request.POST.get(field, "").strip(), request.POST.get(field, "").strip()) for field in ITAssessmentScoringEngine.ALL_FIELDS}
        missing = [field for field, value in answers.items() if not value]
        valid_choices = {value for value, _label in ITAssessmentScoringEngine.OPTIONS}
        invalid = [field for field, value in answers.items() if value and value not in valid_choices]
        if missing or invalid:
            messages.error(request, "Please answer every question. Questions marked Not Available are still considered answered.")
            ITAssessmentDraft.objects.update_or_create(device=device, defaults={"answers": answers, "updated_by": request.user})
        else:
            assessment, _created = ITAssessment.objects.update_or_create(
                device=device,
                defaults={**answers, "assessed_by": request.user, "notes": request.POST.get("notes", "")},
            )
            assessment.completed_at = timezone.now()
            assessment.save(update_fields=["completed_at", "updated_at"])
            ITAssessmentDraft.objects.filter(device=device).delete()
            if device.status == Device.Status.REPAIR_AND_SANITIZATION_PENDING:
                device.status = Device.Status.SANITIZATION_PROCESSING
            else:
                device.status = Device.Status.IT_ADMIN_ASSESMENT_COMPLETE
            device.save(update_fields=["status", "updated_at"])

            dispatch_report = request.POST.get("dispatch_report") == "yes"
            report_dispatched = False
            if dispatch_report and device.owner.email:
                send_mail(
                    subject=f"Verified IT Assessment Report - {device.device_name}",
                    message=(
                        f"Dear {device.owner.get_full_name() or device.owner.username},\n\n"
                        f"The verified IT audit report for {device.device_name} is complete.\n"
                        f"Overall Verified IT Score: {assessment.overall_score:.1f} / 100\n"
                        f"Rating: {assessment.rating_label}\n\n"
                        f"You can also view the full report in the Assessments tab or on the device details page."
                    ),
                    from_email=None,
                    recipient_list=[device.owner.email],
                    fail_silently=False,
                )
                report_dispatched = True

            record_audit(device, request.user, "IT admin assessment complete", assessment.rating_label)

            pathway_action = request.POST.get("pathway_action")
            owner_assessment = getattr(device, "self_assessment", None)
            chosen_pathway = owner_assessment.chosen_pathway if owner_assessment else None
            if pathway_action == "route" and chosen_pathway == "SANITIZATION":
                device.status = Device.Status.SANITIZATION_PROCESSING
                device.sanitization_tag = "pending"
                device.repair_tag = ""
                device.save(update_fields=["status", "sanitization_tag", "repair_tag", "updated_at"])
                record_audit(device, request.user, "Sanitization_Processing", "Owner pathway accepted for sanitization.")
            elif pathway_action == "route" and chosen_pathway == "REPAIR":
                device.status = Device.Status.REPAIR_PROCESSING
                device.repair_tag = "pending"
                device.sanitization_tag = ""
                device.save(update_fields=["status", "repair_tag", "sanitization_tag", "updated_at"])
                record_audit(device, request.user, "Repair_Processing", "Owner pathway accepted for repair.")
            elif pathway_action == "route" and chosen_pathway == "BOTH":
                device.status = Device.Status.REPAIR_AND_SANITIZATION_PENDING
                device.repair_tag = "pending"
                device.sanitization_tag = "pending"
                device.save(update_fields=["status", "repair_tag", "sanitization_tag", "updated_at"])
                record_audit(device, request.user, "REPAIR_AND_SANITIZATION_PENDING", "Owner pathway accepted for repair and sanitization.")

            dispatch_message = " and emailed to the owner" if report_dispatched else "; owner email dispatch was not selected"
            messages.success(request, f"IT verification assessment completed and submitted to the Assessments tab{dispatch_message}.")
            if request.POST.get("save_assessment") == "yes":
                return redirect(f"/laptops/it-assessment/{device.pk}/#verified-audit-summary")
            return redirect("it_assessments_portal")
        initial_answers = answers
        score_assessment = ITAssessment(device=device)
        for field_name, answer in initial_answers.items():
            if hasattr(score_assessment, field_name):
                setattr(score_assessment, field_name, answer)
        verified_score = score_assessment.overall_score

    owner_assessment = getattr(device, "self_assessment", None)
    if owner_assessment:
        disposition_pathway = owner_assessment.get_chosen_pathway_display()
    elif verified_score >= 65:
        disposition_pathway = "Repair & Sanitization" if verified_score < 85 else "Sanitization & Wiping"
    else:
        disposition_pathway = "Repair & Sanitization"

    modules = []
    question_number = 1
    for module_name, (weight, questions) in ITAssessmentScoringEngine.MODULES.items():
        module_questions = [
            (field, label, question_number + offset, initial_answers.get(field, ""))
            for offset, (field, label) in enumerate(questions.items())
        ]
        question_number += len(questions)
        modules.append({"name": module_name, "weight": weight, "questions": module_questions})
    return render(request, "laptops/it_assessment.html", {
        "device": device,
        "assessment": assessment,
        "draft_answers": initial_answers,
        "modules": modules,
        "options": ITAssessmentScoringEngine.OPTIONS,
        "engine": ITAssessmentScoringEngine,
        "verified_score": verified_score,
        "disposition_pathway": disposition_pathway,
        "owner_assessment": owner_assessment,
        "active_devices": active_devices,
        "pending_queue": pending_queue,
    })


@it_admin_required
def save_it_assessment_draft(request, device_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    device = get_object_or_404(Device, pk=device_id)
    import json
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = request.POST.dict()
    answers = payload.get("answers", payload)
    valid_fields = set(ITAssessmentScoringEngine.ALL_FIELDS)
    valid_answers = {value for value, _label in ITAssessmentScoringEngine.OPTIONS} | {label for _value, label in ITAssessmentScoringEngine.OPTIONS} | {"GOOD", "SOMEWHAT", "POOR", "BAD", "NOT_AVAILABLE", "Intermittent", "Not Tested", "Good / Working", "Bad / Spoilt", "Needs Attention"}
    answer_map = {"Working / Good": "GOOD", "Good / Working": "GOOD", "Needs Attention": "SOMEWHAT", "Somewhat Working": "SOMEWHAT", "Intermittent": "SOMEWHAT", "Not Tested": "SOMEWHAT", "Poor / Spoilt": "POOR", "Bad / Spoilt": "POOR", "Not Available": "NOT_AVAILABLE", "GOOD": "GOOD", "SOMEWHAT": "SOMEWHAT", "POOR": "POOR", "NOT_AVAILABLE": "NOT_AVAILABLE"}
    clean_answers = {
        key: answer_map.get(value, value)
        for key, value in answers.items()
        if key in valid_fields and value in valid_answers
    }
    draft, _created = ITAssessmentDraft.objects.get_or_create(device=device, defaults={"updated_by": request.user})
    current = draft.answers if isinstance(draft.answers, dict) else {}
    current.update(clean_answers)
    for key, value in list(payload.items()):
        if key.endswith("__clear") and key[:-7] in valid_fields:
            current.pop(key[:-7], None)
    draft.answers = current
    draft.updated_by = request.user
    draft.save(update_fields=["answers", "updated_by", "updated_at"])
    return JsonResponse({"saved": True, "answered": len(current), "total": ITAssessmentScoringEngine.QUESTION_COUNT})


@it_admin_required
def sanitization(request):
    eligible = Q(self_assessment__chosen_pathway__in=["SANITIZATION", "BOTH"]) & ~Q(status__in=[
        Device.Status.FAILED,
        Device.Status.SANITIZATION_FAILED,
        Device.Status.SANITIZED,
        Device.Status.COMPLETED,
        Device.Status.DONATED,
    ])
    base_devices = Device.objects.filter(eligible).distinct().select_related("owner", "self_assessment", "it_assessment").prefetch_related("sanitization_jobs")
    query = request.GET.get("q", "").strip()
    filter_name = request.GET.get("sanitization_filter", "pending").lower()
    if filter_name not in {"pending", "completed", "failed"}:
        filter_name = "pending"

    pending_queryset = base_devices.exclude(status=Device.Status.DISPOSITION_PENDING).exclude(
        Q(certificate__isnull=False) | Q(sanitization_tag__in=["complete", "completed"])
    )
    failed_queryset = Device.objects.filter(
        self_assessment__chosen_pathway__in=["SANITIZATION", "BOTH"],
    ).filter(
        Q(status=Device.Status.SANITIZATION_FAILED)
        | Q(sanitization_jobs__status=SanitizationJob.FAIL)
    ).distinct().select_related("owner", "self_assessment", "it_assessment").prefetch_related("sanitization_jobs")
    completed_queryset = SanitizationCertificate.objects.select_related("device", "device__owner", "verified_by")

    pending_queryset = pending_queryset.order_by("-updated_at", "-pk")
    failed_queryset = failed_queryset.order_by("-updated_at", "-pk")
    completed_queryset = completed_queryset.order_by("-issued_at", "-pk")

    pending_count = pending_queryset.count()
    completed_count = completed_queryset.count()
    failed_count = failed_queryset.count()

    if query:
        pending_queryset = pending_queryset.filter(
            Q(device_name__icontains=query) | Q(make__icontains=query) | Q(model_name__icontains=query)
            | Q(serial_number__icontains=query) | Q(owner__username__icontains=query)
        )
        failed_queryset = failed_queryset.filter(
            Q(device_name__icontains=query) | Q(make__icontains=query) | Q(model_name__icontains=query)
            | Q(serial_number__icontains=query) | Q(owner__username__icontains=query)
        )
        completed_queryset = completed_queryset.filter(
            Q(device__device_name__icontains=query) | Q(device__make__icontains=query)
            | Q(device__model_name__icontains=query) | Q(device__serial_number__icontains=query)
            | Q(device__owner__username__icontains=query)
        )

    devices = _searched_devices(pending_queryset, query).order_by("-updated_at", "-pk")
    failed_devices = failed_queryset.order_by("-updated_at", "-pk")
    sanitized_certificates = completed_queryset.order_by("-issued_at", "-pk")
    for device in devices:
        try:
            device.assessment_completed = device.it_assessment.completed_at is not None
        except ITAssessment.DoesNotExist:
            device.assessment_completed = False
    if request.method == "POST":
        device = get_object_or_404(Device.objects.filter(eligible).distinct(), pk=request.POST.get("device"))
        action = request.POST.get("action")
        job = device.sanitization_jobs.order_by("-started_at").first()
        if action in {"pass", "fail"}:
            if job is None:
                job = SanitizationJob(device=device)
            job.status = SanitizationJob.PASS if action == "pass" else SanitizationJob.FAIL
            job.save()
            if action == "pass":
                device.status = Device.Status.IT_VERIFIED
                record_audit(device, request.user, "Sanitization mock passed", "Mock wipe test passed; IT verification is required.")
                messages.success(request, "Mock sanitization passed. Verify the result to issue a certificate.")
            else:
                device.sanitization_tag = "complete"
                device.failure_notes = request.POST.get("failure_notes", "Mock sanitization failed.").strip() or "Mock sanitization failed."
                if not _advance_both_stage(device, "sanitization"):
                    device.status = Device.Status.DISPOSITION_PENDING
                record_audit(device, request.user, "Sanitization failed", device.failure_notes)
                messages.error(request, "Mock sanitization failure recorded as a completed outcome.")
            device.save(update_fields=["status", "sanitization_tag", "failure_notes", "updated_at"])
        elif action == "verify" and job and job.status == SanitizationJob.PASS:
            cert_code = uuid.uuid4()
            crypto_hash = hashlib.sha256(f"{device.serial_number}:{cert_code}:{timezone.now().isoformat()}".encode()).hexdigest()
            certificate, _ = SanitizationCertificate.objects.get_or_create(
                device=device,
                defaults={"verified_by": request.user, "cert_code": cert_code, "crypto_hash": crypto_hash},
            )
            job.status = "VERIFIED"
            job.save(update_fields=["status"])
            device.sanitization_tag = "complete"
            if not _advance_both_stage(device, "sanitization"):
                device.status = Device.Status.DISPOSITION_PENDING
            device.save(update_fields=["status", "sanitization_tag", "updated_at"])
            record_audit(device, request.user, "Sanitization verified", f"Certificate {certificate.cert_code} issued; SHA-256 {certificate.crypto_hash}.")
            messages.success(request, "Sanitization verified and marked complete.")
        else:
            messages.error(request, "That sanitization action is not available for this device.")
        return redirect("sanitization")
    return render(request, "laptops/sanitization.html", {
        "devices": devices,
        "failed_devices": failed_devices,
        "sanitized_certificates": sanitized_certificates,
        "pending_count": pending_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "sanitization_filter": filter_name,
        "query": query,
    })


@it_admin_required
def sanitization_execution(request, device_id):
    device = get_object_or_404(
        Device.objects.select_related("owner", "self_assessment", "it_assessment"),
        pk=device_id,
    )
    try:
        assessment_completed = device.it_assessment.completed_at is not None
    except ITAssessment.DoesNotExist:
        assessment_completed = False
    assessed_statuses = {
        Device.Status.IT_ADMIN_ASSESMENT_COMPLETE,
        Device.Status.SANITIZATION_PROCESSING,
        Device.Status.REPAIR_PROCESSING,
        Device.Status.REPAIR_AND_SANITIZATION_PENDING,
    }
    if device.status not in assessed_statuses and not assessment_completed:
        raise Http404

    choices = {
        "standards": [
            "NIST SP 800-88 Rev. 1 Purge",
            "DoD 5220.22-M",
            "ATA Secure Erase",
            "Single-Pass Zero Fill",
        ],
        "tools": [
            "Blancco Drive Eraser",
            "DBAN",
            "Internal UEFI Secure Wipe",
            "Linux dd/nvme-cli",
        ],
        "pass_counts": ["1 Pass", "3 Passes", "7 Passes"],
        "storage_media_types": [
            "HDD (Hard Disk Drive)",
            "SATA SSD",
            "NVMe SSD",
            "M.2 SSD",
            "SSHD (Hybrid Drive)",
            "eMMC Storage",
            "USB Flash Drive",
            "SD Card",
            "microSD Card",
            "CompactFlash Card",
            "Magnetic Tape",
            "Floppy Disk",
            "CD",
            "DVD",
            "Blu-ray Disc",
            "Unknown",
        ],
    }

    if request.method == "POST":
        action = request.POST.get("action")
        standard = request.POST.get("sanitization_standard", "").strip()
        tool = request.POST.get("tool_used", "").strip()
        pass_count = request.POST.get("pass_count", "").strip()
        drive_serial = request.POST.get("drive_serial_number", "").strip()
        media_type = request.POST.get("storage_media_type", "").strip()
        notes = request.POST.get("technician_notes", "").strip()

        if action == "FAIL":
            device.sanitization_tag = "complete"
            device.failure_notes = notes or "Sanitization failed."
            if not _advance_both_stage(device, "sanitization"):
                device.status = Device.Status.DISPOSITION_PENDING
            device.save(update_fields=["status", "sanitization_tag", "failure_notes", "updated_at"])
            SanitizationJob.objects.create(device=device, status=SanitizationJob.FAIL)
            record_audit(device, request.user, "Sanitization failed", notes or "Sanitization failed at the workstation.")
            messages.error(request, "Sanitization failure recorded.")
            return redirect("sanitization")

        required_values = {
            "sanitization_standard": standard,
            "tool_used": tool,
            "pass_count": pass_count,
            "drive_serial_number": drive_serial,
            "storage_media_type": media_type,
        }
        if action != "PASS" or any(not value for value in required_values.values()):
            messages.error(request, "Complete every required sanitization field before generating the certificate.")
            return render(request, "laptops/sanitization_execution.html", {
                "device": device,
                **choices,
                "posted": {**required_values, "technician_notes": notes},
            }, status=400)
        if (
            standard not in choices["standards"]
            or tool not in choices["tools"]
            or pass_count not in choices["pass_counts"]
            or media_type not in choices["storage_media_types"]
        ):
            messages.error(request, "An invalid sanitization option was submitted.")
            return render(request, "laptops/sanitization_execution.html", {"device": device, **choices, "posted": required_values}, status=400)

        cert_code = uuid.uuid4()
        crypto_hash = hashlib.sha256(
            f"{device.serial_number}:{drive_serial}:{cert_code}:{timezone.now().isoformat()}".encode()
        ).hexdigest()
        certificate, _created = SanitizationCertificate.objects.update_or_create(
            device=device,
            defaults={
                "verified_by": request.user,
                "cert_code": cert_code,
                "crypto_hash": crypto_hash,
                "wipe_method": f"{standard} · {tool} · {pass_count}",
                "sanitization_standard": standard,
                "tool_used": tool,
                "pass_count": pass_count,
                "drive_serial_number": drive_serial,
                "storage_media_type": media_type,
                "technician_notes": notes,
            },
        )
        job, _ = SanitizationJob.objects.get_or_create(device=device, defaults={"status": SanitizationJob.PASS})
        job.status = "VERIFIED"
        job.save(update_fields=["status"])
        device.sanitization_tag = "complete"
        device.save(update_fields=["sanitization_tag", "updated_at"])
        if not _advance_both_stage(device, "sanitization"):
            device.status = Device.Status.DISPOSITION_PENDING
            device.save(update_fields=["status", "updated_at"])
        record_audit(
            device,
            request.user,
            "Sanitization certificate issued",
            f"Certificate {certificate.cert_code}: {standard}; {tool}; {pass_count}; drive serial {drive_serial}.",
        )
        return render(request, "laptops/sanitization_certificate.html", {
            "device": device,
            "certificate": certificate,
        })

    return render(request, "laptops/sanitization_execution.html", {"device": device, **choices, "posted": {}})


@login_required
def sanitization_certificate(request, certificate_id):
    certificate = get_object_or_404(
        SanitizationCertificate.objects.select_related("device", "device__owner", "verified_by"),
        pk=certificate_id,
    )
    if certificate.device.owner_id != request.user.pk and not request.user.is_it_admin():
        raise PermissionDenied("You do not have access to this sanitization certificate.")
    return render(request, "laptops/sanitization_certificate.html", {
        "device": certificate.device,
        "certificate": certificate,
    })


def _repair_component_flags(device):
    repairable_statuses = ("PARTIALLY_WORKING", "NOT_WORKING", "MISSING")
    component_labels = {
        "screen_status": "Screen",
        "keyboard_status": "Keyboard",
        "battery_status": "Battery",
        "chassis_status": "Chassis",
        "storage_status": "Storage",
    }
    status_labels = dict(OwnerSelfAssessment.COMPONENT_STATUS_CHOICES)
    owner_flags = []
    diagnostic_flags = []

    try:
        owner_assessment = device.self_assessment
        for field in TechnicalAssessment.COMPONENTS:
            status_code = getattr(owner_assessment, field)
            if status_code in repairable_statuses:
                owner_flags.append((component_labels[field], status_labels[status_code], status_code))
    except OwnerSelfAssessment.DoesNotExist:
        pass

    try:
        diagnostic_assessment = device.it_assessment
        for module_name, (_weight, questions) in ITAssessmentScoringEngine.MODULES.items():
            for field in questions:
                status_code = getattr(diagnostic_assessment, field, "")
                if status_code in ("SOMEWHAT", "POOR", "Somewhat Working", "Poor / Spoilt", "Intermittent", "Bad / Spoilt"):
                    if status_code in ("SOMEWHAT", "Somewhat Working", "Intermittent"):
                        diagnostic_label = "Needs Attention"
                    elif status_code in ("POOR", "Poor / Spoilt", "Bad / Spoilt"):
                        diagnostic_label = "Not Working"
                    else:
                        diagnostic_label = status_code.replace("_", " ").title()
                    diagnostic_flags.append((module_name, diagnostic_label, status_code))
                    break
    except ITAssessment.DoesNotExist:
        pass

    try:
        technical_assessment = device.technical_assessment
        for field in TechnicalAssessment.COMPONENTS:
            status_code = getattr(technical_assessment, field, "")
            if status_code in repairable_statuses:
                diagnostic_flags.append((component_labels[field], dict(TechnicalAssessment.COMPONENT_STATUS_CHOICES)[status_code], status_code))
    except TechnicalAssessment.DoesNotExist:
        pass

    components_by_name = {}
    for component, label, status_code in owner_flags + diagnostic_flags:
        components_by_name.setdefault(component, {"name": component, "owner_status": None, "diagnostic_status": None})
        if (component, label, status_code) in owner_flags:
            components_by_name[component]["owner_status"] = label
        if (component, label, status_code) in diagnostic_flags:
            components_by_name[component]["diagnostic_status"] = label

    return owner_flags, diagnostic_flags, list(components_by_name.values())


@it_admin_required
def repairs(request):
    repairable_statuses = ("PARTIALLY_WORKING", "NOT_WORKING", "MISSING")
    component_labels = {
        "screen_status": "Screen",
        "keyboard_status": "Keyboard",
        "battery_status": "Battery",
        "chassis_status": "Chassis",
        "storage_status": "Storage",
    }
    candidates = _searched_devices(Device.objects.filter(
        self_assessment__chosen_pathway__in=["REPAIR", "BOTH"],
        repairs__isnull=True,
    ).exclude(
        status__in=[Device.Status.COMPLETED, Device.Status.SANITIZED, Device.Status.DONATED]
    ).select_related("owner", "self_assessment", "it_assessment", "technical_assessment").distinct().order_by("-created_at", "-pk"), request.GET.get("q", "").strip())
    devices = []
    status_labels = dict(TechnicalAssessment.COMPONENT_STATUS_CHOICES)
    for device in candidates:
        owner_flags, diagnostic_flags, flagged_components = _repair_component_flags(device)
        original_component_count = len(flagged_components)
        try:
            repair_draft = device.repair_execution_draft
            draft_answers = repair_draft.answers or {}
        except RepairExecutionDraft.DoesNotExist:
            draft_answers = {}

        completed_components = {
            component_name
            for component_name, result in draft_answers.items()
            if result in ("FIXED", "NOT_FIXED")
            and component_name in {component["name"] for component in flagged_components}
        }
        owner_flags = [flag for flag in owner_flags if flag[0] not in completed_components]
        diagnostic_flags = [flag for flag in diagnostic_flags if flag[0] not in completed_components]
        unresolved_components = [component for component in flagged_components if component["name"] not in completed_components]

        def diagnostic_badge_class(status_code):
            normalized = str(status_code).upper()
            if normalized in {"POOR", "NOT_WORKING", "BAD", "BAD / SPOILT"}:
                return "bg-red-100 text-red-700"
            if normalized in {"SOMEWHAT", "PARTIALLY_WORKING", "INTERMITTENT", "NOT TESTED"}:
                return "bg-amber-100 text-amber-700"
            return "bg-blue-100 text-blue-700"

        device.owner_assessment_flags = owner_flags
        device.it_diagnostic_flags = [
            (component, status, status_code, diagnostic_badge_class(status_code))
            for component, status, status_code in diagnostic_flags
        ]
        device.flagged_components = unresolved_components
        try:
            device.assessment_completed = device.it_assessment.completed_at is not None
        except ITAssessment.DoesNotExist:
            device.assessment_completed = False
        try:
            assessment = device.technical_assessment
        except TechnicalAssessment.DoesNotExist:
            assessment = device.self_assessment
        device.repairable_components = [
            (component_labels[field], status_labels[getattr(assessment, field)])
            for field in TechnicalAssessment.COMPONENTS
            if getattr(assessment, field) in repairable_statuses
        ]
        try:
            draft = device.repair_execution_draft
            answered_count = len(set(draft.answers) & {component["name"] for component in flagged_components})
        except RepairExecutionDraft.DoesNotExist:
            answered_count = 0
            draft = None
        answered_count = len(completed_components)
        device.repair_progress = round((answered_count / original_component_count * 100) if original_component_count else 0)
        device.repair_progress_label = "Continue Repair" if draft is not None else "Start Repair"
        device.repair_form = RepairEvaluationForm()
        devices.append(device)

    repaired_devices = []
    repaired_by_device = {}
    repaired_evaluations = RepairEvaluation.objects.select_related(
        "device", "device__owner", "technician"
    ).order_by("-created_at", "-pk")
    query = request.GET.get("q", "").strip()
    if query:
        repaired_evaluations = repaired_evaluations.filter(
            Q(device__device_name__icontains=query)
            | Q(device__make__icontains=query)
            | Q(device__model_name__icontains=query)
            | Q(device__serial_number__icontains=query)
            | Q(device__owner__username__icontains=query)
        )
    for evaluation in repaired_evaluations:
        repaired_by_device.setdefault(evaluation.device_id, {
            "device": evaluation.device,
            "evaluations": [],
            "fixed": [],
            "not_repairable": [],
            "technicians": [],
            "comments": [],
            "completed_at": evaluation.created_at,
        })
        record = repaired_by_device[evaluation.device_id]
        record["evaluations"].append(evaluation)
        if evaluation.repair_result == "FIXED":
            record["fixed"].append(evaluation.component_name)
        else:
            record["not_repairable"].append(evaluation.component_name)
        if evaluation.technician and evaluation.technician not in record["technicians"]:
            record["technicians"].append(evaluation.technician)
        if evaluation.comments and evaluation.comments not in record["comments"]:
            record["comments"].append(evaluation.comments)
    repaired_devices = list(repaired_by_device.values())

    if request.method == "POST":
        device = next((queued_device for queued_device in devices if str(queued_device.pk) == request.POST.get("device")), None)
        if device is None:
            raise Http404
        form = RepairEvaluationForm(request.POST)
        if form.is_valid():
            result = form.cleaned_data["repair_result"]
            comments = form.cleaned_data["comments"]
            for component, _ in device.repairable_components:
                RepairEvaluation.objects.create(
                    device=device,
                    technician=request.user,
                    component_name=component,
                    repair_result=result,
                    comments=comments,
                )
            if result == "NOT_FIXED":
                device.failure_notes = comments
            else:
                device.failure_notes = None
            device.repair_tag = "complete"
            if not _advance_both_stage(device, "repair"):
                device.status = Device.Status.DISPOSITION_PENDING
            device.save(update_fields=["status", "failure_notes", "repair_tag", "updated_at"])
            component_names = ", ".join(component for component, _ in device.repairable_components)
            record_audit(device, request.user, f"Repair evaluation: {form.cleaned_data['repair_result']}", f"{component_names}: {comments}")
            messages.success(request, "Repair evaluation recorded.")
            return redirect("repairs")
        device.repair_form = form
        return render(request, "laptops/repairs.html", {"devices": devices, "active_device": device, "repaired_devices": repaired_devices})
    return render(request, "laptops/repairs.html", {"devices": devices, "repaired_devices": repaired_devices})


@it_admin_required
def repair_execution(request, device_id):
    device = get_object_or_404(Device.objects.select_related("owner", "self_assessment", "it_assessment", "technical_assessment"), pk=device_id)
    try:
        assessment_completed = device.it_assessment.completed_at is not None
    except ITAssessment.DoesNotExist:
        assessment_completed = False
    assessed_statuses = {
        Device.Status.IT_ADMIN_ASSESMENT_COMPLETE,
        Device.Status.REPAIR_PROCESSING,
        Device.Status.SANITIZATION_PROCESSING,
        Device.Status.REPAIR_AND_SANITIZATION_PENDING,
    }
    if device.status not in assessed_statuses and not assessment_completed:
        raise Http404
    _owner_flags, _diagnostic_flags, flagged_components = _repair_component_flags(device)
    original_component_count = len(flagged_components)
    try:
        repair_draft = device.repair_execution_draft
        draft_answers = repair_draft.answers
        draft_comments = repair_draft.comments
    except RepairExecutionDraft.DoesNotExist:
        draft_answers = {}
        draft_comments = ""
    outcome_answers = {
        evaluation.component_name: evaluation.repair_result
        for evaluation in device.repairs.order_by("created_at", "pk")
        if evaluation.repair_result in ("FIXED", "NOT_FIXED")
    }
    outcome_answers.update(draft_answers)
    completed_components = {
        component_name
        for component_name, result in outcome_answers.items()
        if result in ("FIXED", "NOT_FIXED")
        and component_name in {component["name"] for component in flagged_components}
    }
    outcome_answers = {
        component_name: result
        for component_name, result in outcome_answers.items()
        if component_name in completed_components
    }
    flagged_components = [
        component
        for component in flagged_components
        if component["name"] not in completed_components
    ]
    repair_already_finalized = not flagged_components and not draft_answers and bool(outcome_answers)
    if request.method == "POST" and repair_already_finalized:
        messages.info(request, "All flagged component outcomes have already been finalized.")
        return redirect("repairs")

    if request.method == "POST":
        answers = {}
        for component in flagged_components:
            key = "component_" + slugify(component["name"]).replace("-", "")
            value = request.POST.get(key)
            if value in ("FIXED", "NOT_FIXED"):
                answers[component["name"]] = value

        merged_answers = {**outcome_answers, **answers}
        comments = request.POST.get("technician_notes", request.POST.get("comments", draft_comments)).strip()
        RepairExecutionDraft.objects.update_or_create(
            device=device,
            defaults={"answers": merged_answers, "comments": comments, "updated_by": request.user},
        )
        if request.POST.get("save_draft") == "yes":
            return JsonResponse({"saved": True, "answered": len(merged_answers), "total": original_component_count})

        missing_components = []
        results = dict(merged_answers)
        for component in flagged_components:
            key = "component_" + slugify(component["name"]).replace("-", "")
            value = request.POST.get(key)
            if value not in ("FIXED", "NOT_FIXED"):
                missing_components.append(component["name"])
            else:
                results[component["name"]] = value

        comments = request.POST.get("technician_notes", request.POST.get("comments", "")).strip()
        for component in flagged_components:
            component["posted_result"] = results.get(component["name"])
        if missing_components:
            messages.error(request, "Select a repair outcome for every flagged component.")
            return render(request, "laptops/repair_execution.html", {
                "device": device,
                "flagged_components": flagged_components,
                "comments": comments,
                "missing_components": missing_components,
                "posted_results": results,
                "repair_completed_count": len(completed_components),
                "repair_total_count": original_component_count,
                "fixed_components": [name for name, result in results.items() if result == "FIXED"],
                "not_fixed_components": [name for name, result in results.items() if result == "NOT_FIXED"],
            }, status=400)

        for component_name, result in results.items():
            RepairEvaluation.objects.create(
                device=device,
                technician=request.user,
                component_name=component_name,
                repair_result=result,
                comments=comments,
            )
        RepairExecutionDraft.objects.filter(device=device).delete()

        if "NOT_FIXED" in results.values():
            device.failure_notes = comments
        else:
            device.failure_notes = None
        device.repair_tag = "complete"
        device.save(update_fields=["status", "failure_notes", "repair_tag", "updated_at"])
        if not _advance_both_stage(device, "repair"):
            device.status = Device.Status.DISPOSITION_PENDING
            device.save(update_fields=["status", "updated_at"])

        component_names = ", ".join(results.keys())
        record_audit(device, request.user, "Repair execution finalized", f"{component_names}: {comments}")
        messages.success(request, "Repair work finalized and saved.")
        return redirect("repairs")

    for component in flagged_components:
        component["posted_result"] = outcome_answers.get(component["name"])
    return render(request, "laptops/repair_execution.html", {
        "device": device,
        "flagged_components": flagged_components,
        "comments": draft_comments,
        "missing_components": [],
        "posted_results": outcome_answers,
        "repair_completed_count": len(completed_components),
        "repair_total_count": original_component_count,
        "fixed_components": [name for name, result in outcome_answers.items() if result == "FIXED"],
        "not_fixed_components": [name for name, result in outcome_answers.items() if result == "NOT_FIXED"],
        "repair_already_finalized": repair_already_finalized,
    })


@login_required
def disposition(request):
    query = request.GET.get("q", "").strip()
    owned_devices = _searched_devices(Device.objects.filter(
        owner=request.user,
        status__in=[Device.Status.DISPOSITION_PENDING, Device.Status.SCHEDULED_FOR_COLLECTION],
    ).exclude(status__in=[Device.Status.COLLECTED, Device.Status.DONATED]).order_by("-created_at", "-pk"), query)
    if request.user.is_it_admin():
        if request.method == "POST" and request.POST.get("action") in {"approve_donation", "reject_donation", "mark_paid", "approve_slot", "mark_collected"}:
            action = request.POST["action"]
            if action in {"approve_donation", "reject_donation", "mark_paid"}:
                ledger = get_object_or_404(DonationLedger, pk=request.POST.get("entry"))
                if action == "approve_donation" and ledger.payout_status == DonationLedger.Status.PENDING:
                    ledger.payout_status = DonationLedger.Status.APPROVED
                    ledger.rejection_reason = ""
                    ledger.device.disbursement_approved = True
                    ledger.device.save(update_fields=["disbursement_approved", "updated_at"])
                elif action == "reject_donation" and ledger.payout_status == DonationLedger.Status.PENDING:
                    ledger.rejection_reason = request.POST.get("rejection_reason", "").strip()
                    if not ledger.rejection_reason:
                        messages.error(request, "A rejection reason is required before the disbursement can be rejected.")
                        return redirect("disposition")
                    ledger.payout_status = DonationLedger.Status.REJECTED
                    ledger.device.disbursement_approved = False
                    ledger.device.status = Device.Status.DISBURSEMENT_DENIED
                    ledger.device.save(update_fields=["disbursement_approved", "status", "updated_at"])
                elif action == "mark_paid" and ledger.payout_status == DonationLedger.Status.APPROVED:
                    ledger.payout_status = DonationLedger.Status.PAID
                update_fields = ["payout_status"]
                if action in {"approve_donation", "reject_donation"}:
                    update_fields.append("rejection_reason")
                ledger.save(update_fields=update_fields)
                detail = f"Cashback payout {format_kes(ledger.cashback_kes)}."
                if action == "reject_donation" and ledger.rejection_reason:
                    detail += f" Rejection reason: {ledger.rejection_reason}"
                record_audit(ledger.device, request.user, f"Donation {ledger.payout_status}", detail)
            else:
                slot = get_object_or_404(CollectionSlot, pk=request.POST.get("slot"))
                slot.approved = True
                slot.collected = action == "mark_collected"
                slot.save(update_fields=["approved", "collected"])
                if slot.collected:
                    slot.device.status = Device.Status.COLLECTED
                    slot.device.save(update_fields=["status", "updated_at"])
                elif slot.approved:
                    slot.device.status = Device.Status.SCHEDULED_FOR_COLLECTION
                    slot.device.save(update_fields=["status", "updated_at"])
                record_audit(slot.device, request.user, "Collection slot updated", "Pickup marked collected." if slot.collected else "Collection slot approved.")
            messages.success(request, "Disposition record updated.")
            return redirect("disposition")
        donations = DonationLedger.objects.select_related(
            "device",
            "device__owner",
            "device__technical_assessment",
            "device__it_assessment",
            "device__certificate",
            "device__certificate__verified_by",
            "donated_by",
        ).prefetch_related("device__repairs__technician").order_by("-donated_at", "-pk")
        disposition_filter = request.GET.get("disposition_filter", "pending")
        if disposition_filter == "approved_paid":
            donations = donations.filter(
                Q(device__status=Device.Status.SCHEDULED_FOR_COLLECTION)
                | Q(payout_status__in=[DonationLedger.Status.APPROVED, DonationLedger.Status.PAID])
            )
        elif disposition_filter == "denied":
            donations = donations.filter(
                Q(device__status=Device.Status.DISBURSEMENT_DENIED)
                | Q(payout_status=DonationLedger.Status.REJECTED)
            )
        else:
            disposition_filter = "pending"
            donations = donations.filter(
                Q(device__status=Device.Status.DISPOSITION_PENDING)
                | Q(payout_status=DonationLedger.Status.PENDING)
            )
        if query:
            donations = donations.filter(
                Q(device__device_name__icontains=query)
                | Q(device__make__icontains=query)
                | Q(device__model_name__icontains=query)
                | Q(device__serial_number__icontains=query)
                | Q(device__owner__username__icontains=query)
            )
        for entry in donations:
            valuation = calculate_device_valuation(entry.device)
            entry.acquired_value = valuation["acquired_value"]
            entry.current_value = valuation["current_value"]
            entry.cashback_payout = valuation["cashback_amount"]
            component_labels = {
                "Motherboard": "Motherboard",
                "CPU / Processor": "CPU / Processor",
                "Storage HDD / SSD": "Storage & Speed",
                "Power System": "Power & Battery",
                "Display / Screen": "Display & Screen",
                "RAM / Memory": "RAM / Memory",
                "Chassis": "Chassis",
                "Keyboard": "Keyboard",
                "Touchpad": "Touchpad",
                "Network Adapter": "Network Adapter",
                "Cooling System": "Cooling System",
                "Audio / Other": "Audio / Other",
            }
            repair_outcomes = {}
            for repair in entry.device.repairs.all():
                repair_outcomes[repair.component_name.lower().strip()] = repair.repair_result

            component_evaluations = []
            weighted_health = Decimal("0")
            total_weight = sum(SUBSYSTEM_WEIGHTS.values(), Decimal("0"))
            for component_name, _weight in SUBSYSTEM_WEIGHTS.items():
                score = Decimal(str(_score_component(entry.device, component_name)))
                weighted_health += (_weight / total_weight) * score
                matched_outcome = None
                normalized_name = component_name.lower()
                for repaired_name, outcome in repair_outcomes.items():
                    if (
                        normalized_name in repaired_name
                        or repaired_name in normalized_name
                        or repaired_name.startswith(normalized_name.split()[0])
                    ):
                        matched_outcome = outcome
                        break

                if matched_outcome == "FIXED":
                    status = "FIXED / RESTORED"
                    status_class = "green"
                elif matched_outcome == "NOT_FIXED":
                    status = "NOT REPAIRABLE"
                    status_class = "red"
                elif score >= Decimal("0.999"):
                    status = "WORKING / GOOD"
                    status_class = "slate"
                elif score <= Decimal("0"):
                    status = "NOT REPAIRABLE"
                    status_class = "red"
                else:
                    status = "FIXED / RESTORED"
                    status_class = "green"

                percent = max(0, min(100, int(score * Decimal("100"))))
                component_evaluations.append({
                    "name": component_labels[component_name],
                    "percent": percent,
                    "status": status,
                    "status_class": status_class,
                })
            entry.component_evaluations = component_evaluations
            entry.overall_health_percent = max(0, min(100, int(weighted_health * Decimal("100"))))
            if entry.overall_health_percent >= 80:
                entry.overall_health_rating = "Excellent"
                entry.overall_health_class = "emerald"
            elif entry.overall_health_percent >= 60:
                entry.overall_health_rating = "Good"
                entry.overall_health_class = "blue"
            elif entry.overall_health_percent >= 40:
                entry.overall_health_rating = "Fair"
                entry.overall_health_class = "amber"
            else:
                entry.overall_health_rating = "Poor"
                entry.overall_health_class = "red"
        slots = CollectionSlot.objects.select_related("device", "requested_by").order_by("-created_at", "-pk")
        disposition_queue = Device.objects.filter(status=Device.Status.DISPOSITION_PENDING).select_related("owner").order_by("-updated_at", "-pk")
        if query:
            slots = slots.filter(
                Q(device__device_name__icontains=query)
                | Q(device__make__icontains=query)
                | Q(device__model_name__icontains=query)
                | Q(device__serial_number__icontains=query)
                | Q(device__owner__username__icontains=query)
            )
            disposition_queue = _searched_devices(disposition_queue, query)
        return render(request, "laptops/disposition.html", {
            "is_admin": True,
            "donations": donations,
            "slots": slots,
            "disposition_queue": disposition_queue,
            "disposition_filter": disposition_filter,
        })

    if request.method == "POST":
        device = get_object_or_404(owned_devices, pk=request.POST.get("device"))
        action = request.POST.get("action")
        if action == "schedule":
            if device.status != Device.Status.DISPOSITION_PENDING:
                messages.error(request, "This device is no longer awaiting a disposition decision.")
                return redirect("disposition")
            scheduled_for = request.POST.get("scheduled_for")
            location = request.POST.get("location", "Strathmore University").strip()
            try:
                when = datetime.fromisoformat(scheduled_for)
                if timezone.is_naive(when):
                    when = timezone.make_aware(when)
                if when <= timezone.now():
                    raise ValueError
            except (TypeError, ValueError):
                messages.error(request, "Choose a valid future collection date and time.")
                return redirect("disposition")
            slot = CollectionSlot.objects.create(device=device, requested_by=request.user, scheduled_for=when, location=location or "Strathmore University")
            device.status = Device.Status.SCHEDULED_FOR_COLLECTION
            device.is_listed = request.POST.get("list_on_timeline") == "yes"
            device.save(update_fields=["status", "is_listed", "updated_at"])
            record_audit(device, request.user, "Pickup requested", f"Collection requested for {when:%d %b %Y, %H:%M} at {slot.location}.")
            messages.success(request, "Collection request submitted.")
        elif action in {"dispose", "donate"}:
            if device.status != Device.Status.DISPOSITION_PENDING:
                messages.error(request, "This device is no longer awaiting a disposition decision.")
                return redirect("disposition")
            if DonationLedger.objects.filter(device=device).exists():
                messages.error(request, "A donation reward has already been recorded for this device.")
                return redirect("disposition")
            valuation = calculate_device_valuation(device)
            donation_value = valuation["current_value"]
            payout = valuation["cashback_amount"]
            entry = DonationLedger.objects.create(device=device, donated_by=request.user, donation_value_kes=donation_value, cashback_kes=payout, payout_status=DonationLedger.Status.PENDING)
            device.status = Device.Status.DONATED
            device.is_listed = False
            device.save(update_fields=["status", "is_listed", "updated_at"])
            record_audit(device, request.user, "Device donated to Parts Bank", f"3% cashback requested: {format_kes(entry.cashback_kes)}.")
            messages.success(request, f"Donation submitted. Pending cashback: {format_kes(payout)}.")
        return redirect("disposition")
    owner_slots = CollectionSlot.objects.filter(requested_by=request.user).select_related("device").order_by("-created_at", "-pk")
    if query:
        owner_slots = owner_slots.filter(
            Q(device__device_name__icontains=query)
            | Q(device__make__icontains=query)
            | Q(device__model_name__icontains=query)
            | Q(device__serial_number__icontains=query)
        )
    active_slots_by_device = {slot.device_id: slot for slot in owner_slots}
    active_devices = list(owned_devices.select_related("owner", "self_assessment", "it_assessment"))
    for device in active_devices:
        slot = active_slots_by_device.get(device.pk)
        device.days_until_collection = max(0, (timezone.localdate(slot.scheduled_for) - timezone.localdate()).days) if slot else None
        if device.status == Device.Status.DISPOSITION_PENDING:
            device.cashback_amount = calculate_device_valuation(device)["cashback_amount"]
    owner_donations = DonationLedger.objects.filter(donated_by=request.user).select_related(
        "device",
        "device__technical_assessment",
        "device__it_assessment",
        "device__certificate",
    ).prefetch_related("device__repairs__technician").order_by("-donated_at", "-pk")
    if query:
        owner_donations = owner_donations.filter(
            Q(device__device_name__icontains=query)
            | Q(device__make__icontains=query)
            | Q(device__model_name__icontains=query)
            | Q(device__serial_number__icontains=query)
        )
    for entry in owner_donations:
        valuation = calculate_device_valuation(entry.device)
        entry.current_value = valuation["current_value"]
        entry.acquired_value = valuation["acquired_value"]
        entry.cashback_payout = valuation["cashback_amount"]
    return render(request, "laptops/disposition.html", {"is_admin": False, "devices": active_devices, "slots": owner_slots, "donations": owner_donations})


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and request.POST.get("action") == "profile" and form.is_valid():
        form.save()
        messages.success(request, "Your contact details have been updated.")
        return redirect("profile")
    if request.user.is_it_admin():
        pending_admins = User.objects.filter(role="IT_ADMIN", admin_approval_status="PENDING", is_superuser=False)
        assessments = ITAssessment.objects.filter(assessed_by=request.user, completed_at__isnull=False).select_related("device")
        certificates = SanitizationCertificate.objects.filter(verified_by=request.user).select_related("device")

        assessment_devices = Device.objects.filter(
            it_assessment__assessed_by=request.user,
            it_assessment__completed_at__isnull=False,
        ).distinct().select_related("owner").order_by("-created_at", "-pk")
        sanitization_devices = Device.objects.filter(
            certificate__verified_by=request.user,
        ).distinct().select_related("owner").order_by("-created_at", "-pk")
        repair_devices = Device.objects.filter(
            audit_logs__actor=request.user,
            audit_logs__action_stage="Repair execution finalized",
        ).distinct().select_related("owner").order_by("-created_at", "-pk")
        my_devices = _searched_devices(
            Device.objects.filter(owner=request.user).select_related("owner").order_by("-created_at", "-pk"),
            request.GET.get("q", "").strip(),
        )

        assessment_count = assessments.count()
        sanitization_count = certificates.count()
        repair_count = repair_devices.count()

        profile_filter = request.GET.get("filter", "ASSESSMENTS").upper()
        if profile_filter not in {"ASSESSMENTS", "SANITIZATIONS", "REPAIRS"}:
            profile_filter = "ASSESSMENTS"
        processed_by_filter = {
            "ASSESSMENTS": assessment_devices,
            "SANITIZATIONS": sanitization_devices,
            "REPAIRS": repair_devices,
        }
        query = request.GET.get("q", "").strip()
        if query:
            processed_by_filter = {
                key: _searched_devices(queryset, query)
                for key, queryset in processed_by_filter.items()
            }

        context = {
            "form": form,
            "is_admin": True,
            "my_devices": my_devices,
            "pending_admins": pending_admins,
            "assessment_count": assessment_count,
            "completed_assessment_count": assessment_count,
            "sanitization_count": sanitization_count,
            "completed_sanitization_count": sanitization_count,
            "repair_count": repair_count,
            "executed_repair_count": repair_count,
            "processed_devices": processed_by_filter[profile_filter],
            "assessment_devices": assessment_devices,
            "sanitization_devices": sanitization_devices,
            "repair_devices": repair_devices,
            "profile_filter": profile_filter,
        }
        return render(request, "laptops/profile.html", context)
    devices = _searched_devices(
        Device.objects.filter(owner=request.user).order_by("-created_at", "-pk"),
        request.GET.get("q", "").strip(),
    )
    donations = request.user.donation_rewards.select_related("device").order_by("-donated_at", "-pk")
    if request.GET.get("q", "").strip():
        query = request.GET["q"].strip()
        donations = donations.filter(
            Q(device__device_name__icontains=query)
            | Q(device__make__icontains=query)
            | Q(device__model_name__icontains=query)
            | Q(device__serial_number__icontains=query)
        )
    context = {
        "form": form,
        "devices": devices,
        "listed_count": devices.filter(is_listed=True).count(),
        "donations": donations,
        "is_admin": False,
    }
    return render(request, "laptops/profile.html", context)


@it_admin_required
@require_POST
def admin_access_decision(request, user_id, decision):
    user = get_object_or_404(User, pk=user_id, role="IT_ADMIN", admin_approval_status="PENDING", is_superuser=False)
    if decision == "approve":
        user.admin_approval_status = "APPROVED"
        user.save(update_fields=["admin_approval_status"])
    elif decision == "reject":
        user.admin_approval_status = "REJECTED"
        user.save(update_fields=["admin_approval_status"])
    else:
        raise PermissionDenied
    messages.success(request, f"IT Administrator request {decision}d.")
    return redirect("profile")


@login_required
def user_assessments_tab(request):
    if request.method == "POST" and request.POST.get("action") == "accept_cashback_offer":
        device = get_object_or_404(
            Device.objects.filter(owner=request.user, status=Device.Status.IT_VERIFIED),
            pk=request.POST.get("device"),
        )
        if request.POST.get("donation_terms") != "accepted":
            messages.error(request, "You must accept the donation terms before submitting the device.")
            return redirect("user_assessments_tab")
        if DonationLedger.objects.filter(device=device).exists():
            messages.error(request, "A cashback offer has already been accepted for this device.")
            return redirect("user_assessments_tab")

        valuation = calculate_device_valuation(device)
        DonationLedger.objects.create(
            device=device,
            donated_by=request.user,
            donation_value_kes=valuation["current_value"],
            cashback_kes=valuation["cashback_amount"],
            payout_status=DonationLedger.Status.PENDING,
        )
        CollectionSlot.objects.filter(device=device).delete()
        device.status = Device.Status.DONATED
        device.save(update_fields=["status", "updated_at"])
        record_audit(
            device,
            request.user,
            "Device donation accepted",
            f"Owner accepted the voluntary donation and cashback offer of {format_kes(valuation['cashback_amount'])}.",
        )
        messages.success(request, "Offer accepted. Your device has been marked as donated.")
        return redirect("user_assessments_tab")

    devices = (
        Device.objects.filter(owner=request.user)
        .exclude(status=Device.Status.REGISTERED)
        .select_related(
            "owner",
            "self_assessment",
            "it_assessment",
            "technical_assessment",
            "certificate",
            "donation_ledger",
        )
        .prefetch_related("repairs")
        .order_by("-created_at", "-pk")
    )
    devices = _searched_devices(devices, request.GET.get("q", "").strip())
    for device in devices:
        device.assessment_valuation = calculate_device_valuation(device)
        repair_records = list(device.repairs.all())
        assessment = getattr(device, "it_assessment", None)
        device.assessment_repairable_parts = [
            repair.component_name
            for repair in repair_records
            if repair.repair_result == "FIXED"
        ]
        device.assessment_non_repairable_parts = [
            repair.component_name
            for repair in repair_records
            if repair.repair_result == "NOT_FIXED"
        ]
        try:
            post_assessment = device.technical_assessment
        except TechnicalAssessment.DoesNotExist:
            post_assessment = None
        device.post_assessment = post_assessment
        device.has_repair_results = bool(repair_records)
        device.final_assessment_score = (
            round(float(getattr(assessment, "overall_score", 0) or 0), 1)
            if assessment
            else 0.0
        )
        device.final_module_scores = (
            dict(getattr(assessment, "module_scores", {}) or {})
            if assessment
            else {}
        )
        for repair in repair_records:
            if repair.repair_result != "FIXED":
                continue
            repaired_name = repair.component_name.lower().strip()
            for module_name in list(device.final_module_scores.keys()):
                normalized_module = module_name.lower().replace("_", " ")
                module_aliases = (
                    normalized_module,
                    normalized_module.replace("storage hdd / ssd", "storage"),
                    normalized_module.replace("power system", "battery"),
                    normalized_module.replace("display / screen", "screen"),
                )
                if any(
                    repaired_name == alias
                    or alias in repaired_name
                    or repaired_name in alias
                    for alias in module_aliases
                ):
                    device.final_module_scores[module_name] = 100.0
                    break
        if assessment and repair_records:
            earned_weight = 0.0
            available_weight = 0.0
            for module_name, (weight, _questions) in ITAssessmentScoringEngine.MODULES.items():
                score = device.final_module_scores.get(module_name)
                if score is not None:
                    available_weight += float(weight)
                    earned_weight += float(weight) * (float(score) / 100.0)
            if available_weight:
                device.final_assessment_score = round(
                    (earned_weight / available_weight) * 100.0,
                    1,
                )
        if device.final_assessment_score >= 85:
            device.final_assessment_label = "EXCELLENT / REISSUE READY"
            device.final_assessment_tailwind = "bg-emerald-100 text-emerald-700 border-emerald-200"
        elif device.final_assessment_score >= 65:
            device.final_assessment_label = "GOOD / MINOR REPAIR"
            device.final_assessment_tailwind = "bg-blue-100 text-blue-700 border-blue-200"
        elif device.final_assessment_score >= 45:
            device.final_assessment_label = "FAIR / RECOVERY & WIPING"
            device.final_assessment_tailwind = "bg-amber-100 text-amber-700 border-amber-200"
        else:
            device.final_assessment_label = "CRITICAL / DECOMMISSION"
            device.final_assessment_tailwind = "bg-rose-100 text-rose-700 border-rose-200"
        if post_assessment or repair_records:
            device.assessment_stage = "POST_REPAIR"
        elif getattr(device, "it_assessment", None):
            device.assessment_stage = "IT_INITIAL"
        elif getattr(device, "self_assessment", None):
            device.assessment_stage = "OWNER_ONLY"
        else:
            device.assessment_stage = "NONE"
        device.assessment_faulty_parts = []
        if assessment:
            for module_name, (_weight, questions) in ITAssessmentScoringEngine.MODULES.items():
                for field_name in questions:
                    if getattr(assessment, field_name, "") in {"POOR", "SOMEWHAT"}:
                        device.assessment_faulty_parts.append(module_name)
                        break
    return render(request, "laptops/user_assessments.html", {"devices": devices})


@login_required
def self_assessment(request, device_id=None):
    confirming = request.method == "POST" and any((
        request.POST.get("confirm_disposition"),
        request.POST.get("confirm_pathway"),
        request.POST.get("confirm_service_pathway"),
        request.POST.get("confirm"),
        request.POST.get("action") == "confirm",
    ))
    if confirming:
        assessment_id = request.POST.get("assessment_id")
        assessment = get_object_or_404(
            OwnerSelfAssessment.objects.select_related("device"),
            pk=assessment_id,
            device__owner=request.user,
        )
        final_pathway = request.POST.get("final_pathway") or request.POST.get("confirmed_pathway") or request.POST.get("pathway")
        if final_pathway in dict(OwnerSelfAssessment.PATHWAY_CHOICES):
            assessment.chosen_pathway = final_pathway
            assessment.save(update_fields=["chosen_pathway"])
        messages.success(request, "Service decision confirmed. Your device has been submitted to IT.")
        return redirect("my_devices")

    initial_device_id = device_id or request.GET.get("device") or request.POST.get("device")
    initial = {"device": initial_device_id} if initial_device_id else None
    form = OwnerSelfAssessmentForm(request.POST or None, user=request.user, initial=initial)
    submitted_assessment = None

    if request.method == "POST" and form.is_valid():
        submitted = form.save(commit=False)
        device = submitted.device
        if device.owner_id != request.user.id or device.status != Device.Status.REGISTERED:
            form.add_error("device", "Select a registered device that you own.")
        else:
            assessment, _created = OwnerSelfAssessment.objects.update_or_create(device=device, defaults={
                "screen_status": submitted.screen_status,
                "keyboard_status": submitted.keyboard_status,
                "battery_status": submitted.battery_status,
                "chassis_status": submitted.chassis_status,
                "storage_status": submitted.storage_status,
                "chosen_pathway": submitted.chosen_pathway,
                "notes": submitted.notes,
            })
            device.status = Device.Status.PENDING_ADMIN_ASSESMENT
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, "Self-assessment submitted by owner", "Self-assessment submitted by owner")
            submitted_assessment = assessment
            return redirect(f"/laptops/self-assessment/{device.pk}/#diagnostic-summary")

    elif request.method == "GET" and initial_device_id:
        device = Device.objects.filter(pk=initial_device_id, owner=request.user).select_related("self_assessment").first()
        if device and hasattr(device, "self_assessment"):
            submitted_assessment = device.self_assessment

    return render(request, "laptops/self_assessment.html", {
        "form": form,
        "submitted_assessment": submitted_assessment,
    })


def sign_in(request):
    if request.user.is_authenticated:
        return redirect("home")
    from django.contrib.auth.forms import AuthenticationForm
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        if user.role == "IT_ADMIN" and user.admin_approval_status != "APPROVED" and not user.is_superuser:
            messages.warning(request, "Your IT Administrator access request is awaiting approval.")
            return redirect("login")
        login(request, user)
        return redirect("home")
    return render(request, "laptops/login.html", {"form": form})


@require_POST
def sign_out(request):
    logout(request)
    return redirect("login")


def register(request):
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        if user.admin_approval_status == "APPROVED":
            login(request, user)
            messages.success(request, "Your account is ready.")
            return redirect("home")
        messages.success(request, "Your IT Administrator access request is awaiting approval.")
        return redirect("login")
    return render(request, "laptops/register.html", {"form": form})


@login_required
def certificate_qr(request, certificate_id):
    certificate = get_object_or_404(SanitizationCertificate.objects.select_related("device"), pk=certificate_id)
    if not request.user.is_it_admin() and certificate.device.owner_id != request.user.pk:
        raise PermissionDenied
    image = qrcode.make(f"{certificate.cert_code}|{certificate.crypto_hash}")
    output = BytesIO()
    image.save(output, format="PNG")
    return HttpResponse(output.getvalue(), content_type="image/png")
