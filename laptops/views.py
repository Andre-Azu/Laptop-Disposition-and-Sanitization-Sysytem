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
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.utils.text import slugify

from .forms import DeviceRegistrationForm, OwnerSelfAssessmentForm, ProfileForm, RegistrationForm, RepairEvaluationForm, TechnicalAssessmentForm
from .models import AuditLog, CollectionSlot, Device, DonationLedger, ITAssessment, ITAssessmentDraft, ITAssessmentScoringEngine, OwnerSelfAssessment, RepairEvaluation, SanitizationCertificate, SanitizationJob, TechnicalAssessment

User = get_user_model()


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


def _searched_devices(queryset, query):
    if query:
        queryset = queryset.filter(Q(device_name__icontains=query) | Q(make__icontains=query) | Q(model_name__icontains=query) | Q(serial_number__icontains=query) | Q(owner__username__icontains=query))
    return queryset


def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if request.user.is_it_admin():
        return redirect("overview")
    devices = Device.objects.filter(owner=request.user)
    public_inventory = _searched_devices(Device.objects.exclude(owner=request.user).exclude(status__in=[Device.Status.FAILED, Device.Status.SANITIZATION_FAILED, Device.Status.DONATED]).select_related("owner", "self_assessment", "technical_assessment"), request.GET.get("q", "").strip())
    reward_total = request.user.donation_rewards.filter(payout_status=DonationLedger.Status.PAID).aggregate(total=Sum("cashback_kes"))["total"] or Decimal("0.00")
    recovered = DonationLedger.objects.filter(payout_status=DonationLedger.Status.PAID).aggregate(total=Sum("donation_value_kes"))["total"] or Decimal("0.00")
    return render(request, "laptops/dashboard.html", {"devices": devices, "inventory": public_inventory, "asset_count": devices.count(), "reward_total": reward_total, "recovered_total": recovered})


@it_admin_required
def overview(request):
    base = _searched_devices(Device.objects.select_related("owner", "self_assessment", "technical_assessment"), request.GET.get("q", "").strip())
    status_filter = request.GET.get("status", "")
    if status_filter:
        base = base.filter(status=status_filter)
    context = {
        "devices": base,
        "total_assets": Device.objects.count(),
        "awaiting_assessment": Device.objects.filter(status=Device.Status.REGISTERED).count(),
        "pending_processing": Device.objects.filter(status=Device.Status.PENDING_PROCESSING).count(),
        "sanitization_completed": Device.objects.filter(status=Device.Status.SANITIZED).count(),
        "failed_sanitizations": SanitizationJob.objects.filter(status=SanitizationJob.FAIL).values("device_id").distinct().count(),
        "ewaste_recovered": DonationLedger.objects.filter(payout_status=DonationLedger.Status.PAID).aggregate(total=Sum("donation_value_kes"))["total"] or Decimal("0.00"),
        "status_choices": Device.STATUS_CHOICES,
    }
    return render(request, "laptops/overview.html", context)


@it_admin_required
def device_inventory(request):
    devices = _searched_devices(Device.objects.select_related("owner", "self_assessment", "technical_assessment", "it_assessment").prefetch_related("audit_logs"), request.GET.get("q", "").strip())
    component = request.GET.get("component", "")
    condition = request.GET.get("condition", "")
    assessment_component = {"screen": "screen_status", "keyboard": "keyboard_status", "battery": "battery_status", "chassis": "chassis_status", "storage": "storage_status"}.get(component)
    conditions = TechnicalAssessment.COMPONENT_STATUS_CHOICES[:3]
    condition_values = {value for value, _ in conditions}
    if assessment_component in TechnicalAssessment.COMPONENTS and condition in condition_values:
        devices = devices.filter(**{f"technical_assessment__{assessment_component}": condition})
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
        return redirect("my_devices")
    return render(request, "laptops/device_register.html", {
        "form": form,
        "model_series": dict(Device.MODEL_SERIES),
        "year_range": range(current_year, current_year - 15, -1),
        "current_year": current_year,
    })


@login_required
def my_devices(request):
    devices = Device.objects.filter(owner=request.user).select_related("owner", "self_assessment").prefetch_related("audit_logs")
    return render(request, "laptops/my_devices.html", {"devices": devices})


@login_required
def device_detail(request, device_id):
    device = get_object_or_404(
        Device.objects.select_related("owner", "self_assessment", "technical_assessment", "it_assessment"),
        pk=device_id,
    )
    if device.owner != request.user and not request.user.is_it_admin():
        raise PermissionDenied("You do not have access to this device.")
    return render(request, "laptops/device_detail.html", {"device": device})


@it_admin_required
def assessments(request):
    active_devices = Device.objects.filter(
        status__in=[Device.Status.PENDING_PROCESSING, Device.Status.IT_IN_PROGRESS]
    ).select_related("owner").order_by("-created_at", "-pk")
    devices = active_devices

    portal_queryset = Device.objects.exclude(
        status=Device.Status.REGISTERED
    ).select_related("owner", "self_assessment", "it_assessment", "it_assessment_draft")

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
        portal_devices.append({"device": device, "category": category, "assessment": assessment})

    pending_queue = []
    for device in Device.objects.filter(status=Device.Status.IT_IN_PROGRESS).select_related("owner", "it_assessment_draft"):
        draft = getattr(device, "it_assessment_draft", None)
        answers = draft.answers if draft and isinstance(draft.answers, dict) else {}
        answered = sum(1 for field in ITAssessmentScoringEngine.ALL_FIELDS if field in answers and answers[field])
        progress = round(min(answered, 55) / 55 * 100)
        pending_queue.append({"device": device, "progress": progress})

    counts = {
        "all": len(portal_devices),
        "pending": sum(1 for item in portal_devices if item["category"] == "pending"),
        "incomplete": sum(1 for item in portal_devices if item["category"] == "incomplete"),
        "completed": sum(1 for item in portal_devices if item["category"] == "completed"),
    }

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
    return render(request, "laptops/assessment_report.html", {
        "device": device,
        "assessment": assessment,
    })


@it_admin_required
def it_assessment(request, device_id=None):
    active_devices = Device.objects.filter(status__in=[Device.Status.PENDING_ADMIN_ASSESMENT, Device.Status.IT_ADMIN_ASSESMENT_IN_PROGRESS]).select_related("owner", "it_assessment_draft")
    if device_id:
        device = Device.objects.select_related("owner", "it_assessment_draft", "it_assessment").filter(pk=device_id).first()
        if device is None:
            messages.warning(request, "That device no longer exists or the link is outdated.")
            return redirect("it_assessments_portal")
        if device.status not in [Device.Status.PENDING_ADMIN_ASSESMENT, Device.Status.IT_ADMIN_ASSESMENT_IN_PROGRESS]:
            messages.warning(request, "Only pending or in-progress devices can be assessed.")
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
        progress = round(min(answer_count, 55) / 55 * 100)
        pending_queue.append({"device": queue_device, "progress": progress})
    draft = getattr(device, "it_assessment_draft", None)
    legacy_answer_map = {
        "GOOD": "GOOD", "Good / Working": "GOOD", "Working / Good": "GOOD",
        "SOMEWHAT": "SOMEWHAT", "Somewhat Working": "SOMEWHAT", "Intermittent": "SOMEWHAT", "Not Tested": "SOMEWHAT",
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
            "SOMEWHAT": "SOMEWHAT", "Somewhat Working": "SOMEWHAT", "Intermittent": "SOMEWHAT", "Not Tested": "SOMEWHAT",
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
            dispatch_message = " and emailed to the owner" if report_dispatched else "; owner email dispatch was not selected"
            messages.success(request, f"IT verification assessment completed and submitted to the Assessments tab{dispatch_message}.")
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
    valid_answers = {value for value, _label in ITAssessmentScoringEngine.OPTIONS} | {label for _value, label in ITAssessmentScoringEngine.OPTIONS} | {"GOOD", "SOMEWHAT", "POOR", "BAD", "NOT_AVAILABLE", "Intermittent", "Not Tested", "Good / Working", "Bad / Spoilt"}
    answer_map = {"Working / Good": "GOOD", "Good / Working": "GOOD", "Somewhat Working": "SOMEWHAT", "Intermittent": "SOMEWHAT", "Not Tested": "SOMEWHAT", "Poor / Spoilt": "POOR", "Bad / Spoilt": "POOR", "Not Available": "NOT_AVAILABLE", "GOOD": "GOOD", "SOMEWHAT": "SOMEWHAT", "POOR": "POOR", "NOT_AVAILABLE": "NOT_AVAILABLE"}
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
    return JsonResponse({"saved": True, "answered": len(current), "total": 55})


@it_admin_required
def sanitization(request):
    eligible = Q(self_assessment__chosen_pathway__in=["SANITIZATION", "BOTH"]) & ~Q(status__in=[Device.Status.SANITIZED, Device.Status.FAILED, Device.Status.SANITIZATION_FAILED, Device.Status.COMPLETED, Device.Status.DONATED])
    devices = Device.objects.filter(eligible).distinct().select_related("owner").prefetch_related("sanitization_jobs")
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
                device.status = Device.Status.SANITIZATION_FAILED
                device.failure_notes = request.POST.get("failure_notes", "Mock sanitization failed.").strip() or "Mock sanitization failed."
                record_audit(device, request.user, "Sanitization failed", device.failure_notes)
                messages.error(request, "Mock sanitization failed. Device moved to the failed queue.")
            device.save(update_fields=["status", "updated_at"])
            if action == "fail":
                device.save(update_fields=["failure_notes"])
        elif action == "verify" and job and job.status == SanitizationJob.PASS:
            cert_code = uuid.uuid4()
            crypto_hash = hashlib.sha256(f"{device.serial_number}:{cert_code}:{timezone.now().isoformat()}".encode()).hexdigest()
            certificate, _ = SanitizationCertificate.objects.get_or_create(
                device=device,
                defaults={"verified_by": request.user, "cert_code": cert_code, "crypto_hash": crypto_hash},
            )
            job.status = "VERIFIED"
            job.save(update_fields=["status"])
            device.status = Device.Status.SANITIZED
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, "Sanitization verified", f"Certificate {certificate.cert_code} issued; SHA-256 {certificate.crypto_hash}.")
            messages.success(request, "Sanitization verified and marked complete.")
        else:
            messages.error(request, "That sanitization action is not available for this device.")
        return redirect("sanitization")
    return render(request, "laptops/sanitization.html", {"devices": devices})


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
        diagnostic_assessment = device.technical_assessment
        for field in TechnicalAssessment.COMPONENTS:
            status_code = getattr(diagnostic_assessment, field)
            if status_code in repairable_statuses:
                diagnostic_flags.append((component_labels[field], status_labels[status_code], status_code))
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
    candidates = Device.objects.filter(
        self_assessment__chosen_pathway__in=["REPAIR", "BOTH"],
        repairs__isnull=True,
    ).exclude(
        status__in=[Device.Status.COMPLETED, Device.Status.SANITIZED, Device.Status.DONATED]
    ).select_related("owner", "self_assessment", "technical_assessment")
    devices = []
    status_labels = dict(TechnicalAssessment.COMPONENT_STATUS_CHOICES)
    for device in candidates:
        owner_flags, diagnostic_flags, flagged_components = _repair_component_flags(device)
        device.owner_assessment_flags = owner_flags
        device.it_diagnostic_flags = diagnostic_flags
        device.flagged_components = flagged_components
        try:
            assessment = device.technical_assessment
        except TechnicalAssessment.DoesNotExist:
            assessment = device.self_assessment
        device.repairable_components = [
            (component_labels[field], status_labels[getattr(assessment, field)])
            for field in TechnicalAssessment.COMPONENTS
            if getattr(assessment, field) in repairable_statuses
        ]
        if device.repairable_components:
            device.repair_form = RepairEvaluationForm()
            devices.append(device)
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
                device.status = Device.Status.FAILED
                device.failure_notes = comments
                device.save(update_fields=["failure_notes"])
            else:
                device.status = Device.Status.PENDING_PROCESSING
            device.save(update_fields=["status", "updated_at"])
            component_names = ", ".join(component for component, _ in device.repairable_components)
            record_audit(device, request.user, f"Repair evaluation: {form.cleaned_data['repair_result']}", f"{component_names}: {comments}")
            messages.success(request, "Repair evaluation recorded.")
            return redirect("repairs")
        device.repair_form = form
        return render(request, "laptops/repairs.html", {"devices": devices, "active_device": device})
    return render(request, "laptops/repairs.html", {"devices": devices})


@it_admin_required
def repair_execution(request, device_id):
    device = get_object_or_404(Device.objects.select_related("owner", "self_assessment", "technical_assessment"), pk=device_id)
    _owner_flags, _diagnostic_flags, flagged_components = _repair_component_flags(device)

    if request.method == "POST":
        missing_components = []
        results = {}
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
            }, status=400)

        for component_name, result in results.items():
            RepairEvaluation.objects.create(
                device=device,
                technician=request.user,
                component_name=component_name,
                repair_result=result,
                comments=comments,
            )

        if "NOT_FIXED" in results.values():
            device.status = Device.Status.FAILED
            device.failure_notes = comments
            device.save(update_fields=["status", "failure_notes", "updated_at"])
        else:
            device.status = Device.Status.PENDING_PROCESSING
            device.save(update_fields=["status", "updated_at"])

        component_names = ", ".join(results.keys())
        record_audit(device, request.user, "Repair execution finalized", f"{component_names}: {comments}")
        messages.success(request, "Repair work finalized and saved.")
        return redirect("repairs")

    return render(request, "laptops/repair_execution.html", {
        "device": device,
        "flagged_components": flagged_components,
        "comments": "",
        "missing_components": [],
        "posted_results": {},
    })


@login_required
def disposition(request):
    owned_devices = Device.objects.filter(
        owner=request.user,
        status__in=[Device.Status.IT_ADMIN_ASSESMENT_COMPLETE, Device.Status.SCHEDULED_FOR_COLLECTION],
    ).exclude(status__in=[Device.Status.COLLECTED, Device.Status.DONATED]).order_by("-created_at", "-pk")
    if request.user.is_it_admin():
        if request.method == "POST" and request.POST.get("action") in {"approve_donation", "reject_donation", "mark_paid", "approve_slot", "mark_collected"}:
            action = request.POST["action"]
            if action in {"approve_donation", "reject_donation", "mark_paid"}:
                ledger = get_object_or_404(DonationLedger, pk=request.POST.get("entry"))
                if action == "approve_donation" and ledger.payout_status == DonationLedger.Status.PENDING:
                    ledger.payout_status = DonationLedger.Status.APPROVED
                elif action == "reject_donation" and ledger.payout_status == DonationLedger.Status.PENDING:
                    ledger.payout_status = DonationLedger.Status.REJECTED
                elif action == "mark_paid" and ledger.payout_status == DonationLedger.Status.APPROVED:
                    ledger.payout_status = DonationLedger.Status.PAID
                ledger.save(update_fields=["payout_status"])
                record_audit(ledger.device, request.user, f"Donation {ledger.payout_status}", f"Cashback payout KES {ledger.cashback_kes}.")
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
        donations = DonationLedger.objects.select_related("device", "donated_by").order_by("-donated_at", "-pk")
        slots = CollectionSlot.objects.select_related("device", "requested_by").order_by("-created_at", "-pk")
        return render(request, "laptops/disposition.html", {"is_admin": True, "donations": donations, "slots": slots})

    if request.method == "POST":
        device = get_object_or_404(owned_devices, pk=request.POST.get("device"))
        action = request.POST.get("action")
        if action == "schedule":
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
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, "Pickup requested", f"Collection requested for {when:%d %b %Y, %H:%M} at {slot.location}.")
            messages.success(request, "Collection request submitted.")
        elif action == "donate":
            if DonationLedger.objects.filter(device=device).exists():
                messages.error(request, "A donation reward has already been recorded for this device.")
                return redirect("disposition")
            donation_value = Decimal(str(device.current_net_valuation)).quantize(Decimal("0.01"))
            payout = (donation_value * Decimal("0.03")).quantize(Decimal("0.01"))
            entry = DonationLedger.objects.create(device=device, donated_by=request.user, donation_value_kes=donation_value, cashback_kes=payout, payout_status=DonationLedger.Status.PENDING)
            device.status = Device.Status.DONATED
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, "Device donated to Parts Bank", f"3% cashback requested: KES {entry.cashback_kes}.")
            messages.success(request, f"Donation submitted. Pending cashback: KES {payout}.")
        return redirect("disposition")
    owner_slots = CollectionSlot.objects.filter(requested_by=request.user).select_related("device").order_by("-created_at", "-pk")
    active_slots_by_device = {slot.device_id: slot for slot in owner_slots}
    active_devices = list(owned_devices.select_related("owner", "self_assessment", "it_assessment"))
    for device in active_devices:
        slot = active_slots_by_device.get(device.pk)
        device.days_until_collection = max(0, (timezone.localdate(slot.scheduled_for) - timezone.localdate()).days) if slot else None
    return render(request, "laptops/disposition.html", {"is_admin": False, "devices": active_devices, "slots": owner_slots, "donations": DonationLedger.objects.filter(donated_by=request.user).select_related("device").order_by("-donated_at", "-pk")})


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and request.POST.get("action") == "profile" and form.is_valid():
        form.save()
        messages.success(request, "Your contact details have been updated.")
        return redirect("profile")
    if request.user.is_it_admin():
        pending_admins = User.objects.filter(role="IT_ADMIN", admin_approval_status="PENDING", is_superuser=False)
        processed = Device.objects.filter(audit_logs__actor=request.user).distinct().select_related("owner")
        certificates = SanitizationCertificate.objects.filter(verified_by=request.user)
        verified_logs = AuditLog.objects.filter(actor=request.user, action_stage="Sanitization verified")
        sanitization_count = SanitizationJob.objects.filter(device__certificate__verified_by=request.user).count()
        context = {"form": form, "pending_admins": pending_admins, "processed_devices": processed, "verification_count": verified_logs.count(), "sanitization_count": sanitization_count, "certificate_count": certificates.count()}
        return render(request, "laptops/profile.html", context)
    devices = Device.objects.filter(owner=request.user)
    context = {"form": form, "devices": devices, "donations": request.user.donation_rewards.select_related("device"), "is_admin": False}
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
    devices = (
        Device.objects.filter(owner=request.user)
        .exclude(status=Device.Status.REGISTERED)
        .select_related("owner", "self_assessment", "it_assessment")
        .order_by("-created_at", "-pk")
    )
    return render(request, "laptops/user_assessments.html", {"devices": devices})


@login_required
def self_assessment(request):
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

    initial_device_id = request.GET.get("device") or request.POST.get("device")
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


@it_admin_required
def certificates(request):
    issued = SanitizationCertificate.objects.select_related("device", "verified_by").order_by("-issued_at")
    return render(request, "laptops/certificates.html", {"certificates": issued})


@login_required
def certificate_qr(request, certificate_id):
    certificate = get_object_or_404(SanitizationCertificate.objects.select_related("device"), pk=certificate_id)
    if not request.user.is_it_admin() and certificate.device.owner_id != request.user.pk:
        raise PermissionDenied
    image = qrcode.make(f"{certificate.cert_code}|{certificate.crypto_hash}")
    output = BytesIO()
    image.save(output, format="PNG")
    return HttpResponse(output.getvalue(), content_type="image/png")
