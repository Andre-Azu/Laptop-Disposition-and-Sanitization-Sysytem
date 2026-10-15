from decimal import Decimal
from datetime import datetime
from io import BytesIO
import hashlib
import uuid
import qrcode
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import DeviceRegistrationForm, OwnerSelfAssessmentForm, ProfileForm, RegistrationForm, RepairEvaluationForm, TechnicalAssessmentForm
from .models import AuditLog, CollectionSlot, Device, DonationLedger, OwnerSelfAssessment, RepairEvaluation, SanitizationCertificate, SanitizationJob, TechnicalAssessment

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
    public_inventory = _searched_devices(Device.objects.exclude(owner=request.user).exclude(status__in=[Device.Status.FAILED, Device.Status.DONATED]).select_related("owner"), request.GET.get("q", "").strip())
    reward_total = request.user.donation_rewards.filter(payout_status=DonationLedger.Status.PAID).aggregate(total=Sum("cashback_kes"))["total"] or Decimal("0.00")
    recovered = DonationLedger.objects.filter(payout_status=DonationLedger.Status.PAID).aggregate(total=Sum("donation_value_kes"))["total"] or Decimal("0.00")
    return render(request, "laptops/dashboard.html", {"devices": devices, "inventory": public_inventory, "asset_count": devices.count(), "reward_total": reward_total, "recovered_total": recovered})


@it_admin_required
def overview(request):
    base = _searched_devices(Device.objects.select_related("owner"), request.GET.get("q", "").strip())
    status_filter = request.GET.get("status", "")
    if status_filter:
        base = base.filter(status=status_filter)
    context = {
        "devices": base,
        "total_assets": Device.objects.count(),
        "awaiting_assessment": Device.objects.filter(status=Device.Status.REGISTERED).count(),
        "pending_processing": Device.objects.filter(status=Device.Status.PENDING_PROCESSING).count(),
        "it_verified": Device.objects.filter(status=Device.Status.IT_VERIFIED).count(),
        "sanitization_completed": Device.objects.filter(status=Device.Status.SANITIZED).count(),
        "failed_sanitizations": SanitizationJob.objects.filter(status=SanitizationJob.FAIL).values("device_id").distinct().count(),
        "ewaste_recovered": DonationLedger.objects.filter(payout_status=DonationLedger.Status.PAID).aggregate(total=Sum("donation_value_kes"))["total"] or Decimal("0.00"),
        "status_choices": Device.STATUS_CHOICES,
    }
    return render(request, "laptops/overview.html", context)


@it_admin_required
def device_inventory(request):
    devices = _searched_devices(Device.objects.select_related("owner"), request.GET.get("q", "").strip())
    component = request.GET.get("component", "")
    minimum = request.GET.get("minimum", "")
    assessment_component = {"screen": "screen_status", "keyboard": "keyboard_status", "battery": "battery_status", "chassis": "chassis_status", "storage": "storage_status"}.get(component)
    if assessment_component in TechnicalAssessment.COMPONENTS and minimum.isdigit():
        threshold = int(minimum)
        qualifying = []
        for device in devices:
            assessment = TechnicalAssessment.objects.filter(device=device).first()
            result = getattr(assessment, assessment_component, "")
            score = {"WORKING": 10, "PARTIALLY_WORKING": 5}.get(result, 0)
            if score >= threshold:
                qualifying.append(device.pk)
        devices = devices.filter(pk__in=qualifying)
    components = [("screen", "Screen"), ("keyboard", "Keyboard"), ("battery", "Battery"), ("chassis", "Chassis"), ("storage", "Storage")]
    return render(request, "laptops/devices.html", {"devices": devices, "components": components})


@login_required
def register_device(request):
    form = DeviceRegistrationForm(request.POST or None, owner=request.user)
    if request.method == "POST" and form.is_valid():
        device = form.save(commit=False)
        device.owner = request.user
        device.status = Device.Status.REGISTERED
        device.save()
        record_audit(device, request.user, "Device registered", f"{device.device_name} added to the recovery system.")
        messages.success(request, "Device registered successfully.")
        return redirect("my_devices")
    return render(request, "laptops/register_device.html", {"form": form, "model_series": dict(Device.MODEL_SERIES)})


@login_required
def my_devices(request):
    devices = Device.objects.filter(owner=request.user).prefetch_related("audit_logs")
    return render(request, "laptops/my_devices.html", {"devices": devices})


@it_admin_required
def assessments(request):
    terminal_statuses = [Device.Status.SANITIZED, Device.Status.COMPLETED]
    devices = Device.objects.select_related("owner").exclude(status__in=terminal_statuses)
    selected_id = request.POST.get("device") or request.GET.get("device")
    selected = None
    if selected_id:
        selected = get_object_or_404(Device.objects.select_related("owner"), pk=selected_id)
        if selected.status in terminal_statuses:
            messages.warning(request, "Sanitized or completed devices cannot be reassessed.")
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
    return render(request, "laptops/assessments.html", {"devices": devices, "selected": selected, "form": form, "latest_assessments": TechnicalAssessment.objects.select_related("device", "assessed_by").order_by("-created_at")[:15]})


@it_admin_required
def sanitization(request):
    eligible = Q(self_assessment__chosen_pathway__in=["SANITIZATION", "BOTH"]) & ~Q(status__in=[Device.Status.SANITIZED, Device.Status.FAILED, Device.Status.COMPLETED, Device.Status.DONATED])
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
                device.status = Device.Status.FAILED
                device.failure_notes = request.POST.get("failure_notes", "Mock sanitization failed.").strip() or "Mock sanitization failed."
                record_audit(device, request.user, "Sanitization failed", device.failure_notes)
                messages.error(request, "Mock sanitization failed. Device moved to the failed queue.")
            device.save(update_fields=["status", "updated_at"])
            if action == "fail":
                device.save(update_fields=["failure_notes"])
        elif action == "verify" and job and job.status == SanitizationJob.PASS:
            cert_code = uuid.uuid4()
            crypto_hash = hashlib.sha256(f"{device.serial_number}:{cert_code}:{timezone.now().isoformat()}".encode()).hexdigest()
            certificate = SanitizationCertificate.objects.create(device=device, verified_by=request.user, cert_code=cert_code, crypto_hash=crypto_hash)
            job.status = "VERIFIED"
            job.save(update_fields=["status"])
            device.status = Device.Status.SANITIZED
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, "Sanitization verified", f"Certificate {certificate.cert_code} issued; SHA-256 {certificate.crypto_hash}.")
            messages.success(request, "Wipe verified and certificate issued.")
        else:
            messages.error(request, "That sanitization action is not available for this device.")
        return redirect("sanitization")
    return render(request, "laptops/sanitization.html", {"devices": devices})


@it_admin_required
def repairs(request):
    repair_queue = Q(self_assessment__chosen_pathway__in=["REPAIR", "BOTH"]) & ~Q(status__in=[Device.Status.COMPLETED, Device.Status.SANITIZED, Device.Status.DONATED])
    devices = Device.objects.filter(repair_queue).distinct().select_related("owner")
    if request.method == "POST":
        device = get_object_or_404(Device.objects.filter(repair_queue).distinct(), pk=request.POST.get("device"))
        form = RepairEvaluationForm(request.POST)
        if form.is_valid():
            evaluation = form.save(commit=False)
            evaluation.device = device
            evaluation.technician = request.user
            evaluation.save()
            if evaluation.repair_result == "NOT_FIXED":
                device.status = Device.Status.FAILED
                device.failure_notes = evaluation.comments
                device.save(update_fields=["failure_notes"])
            else:
                device.status = Device.Status.PENDING_PROCESSING
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, f"Repair evaluation: {evaluation.get_repair_result_display()}", f"{evaluation.component_name}: {evaluation.comments}")
            messages.success(request, "Repair evaluation recorded.")
            return redirect("repairs")
        return render(request, "laptops/repairs.html", {"devices": devices, "form": form, "active_device": device})
    return render(request, "laptops/repairs.html", {"devices": devices, "form": RepairEvaluationForm()})


@login_required
def disposition(request):
    owned_devices = Device.objects.filter(owner=request.user)
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
                record_audit(slot.device, request.user, "Collection slot updated", "Pickup marked collected." if slot.collected else "Collection slot approved.")
            messages.success(request, "Disposition record updated.")
            return redirect("disposition")
        donations = DonationLedger.objects.select_related("device", "donated_by").order_by("-donated_at")
        slots = CollectionSlot.objects.select_related("device", "requested_by").order_by("scheduled_for")
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
    return render(request, "laptops/disposition.html", {"is_admin": False, "devices": owned_devices, "slots": CollectionSlot.objects.filter(requested_by=request.user).select_related("device"), "donations": DonationLedger.objects.filter(donated_by=request.user).select_related("device")})


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
def self_assessment(request):
    initial = {"device": request.GET.get("device")} if request.GET.get("device") else None
    form = OwnerSelfAssessmentForm(request.POST or None, user=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        assessment = form.save(commit=False)
        device = assessment.device
        OwnerSelfAssessment.objects.update_or_create(device=device, defaults={
            "screen_status": assessment.screen_status,
            "keyboard_status": assessment.keyboard_status,
            "battery_status": assessment.battery_status,
            "chassis_status": assessment.chassis_status,
            "storage_status": assessment.storage_status,
            "chosen_pathway": assessment.chosen_pathway,
            "notes": assessment.notes,
        })
        device.status = Device.Status.SELF_ASSESSED
        device.save(update_fields=["status", "updated_at"])
        record_audit(device, request.user, "Owner self-assessment submitted", f"Selected pathway: {assessment.chosen_pathway}.")
        messages.success(request, "Self-assessment submitted. Your device is in the relevant service queue.")
        return redirect("my_devices")
    return render(request, "laptops/self_assessment.html", {"form": form})


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
