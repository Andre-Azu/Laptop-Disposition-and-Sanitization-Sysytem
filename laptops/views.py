from decimal import Decimal
from datetime import datetime
from io import BytesIO
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

from .forms import DeviceRegistrationForm, ProfileForm, RegistrationForm, RepairEvaluationForm, SelfAssessmentForm, TechnicalAssessmentForm
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
    return AuditLog.objects.create(device=device, actor=actor, action=action, details=details)


def _searched_devices(queryset, query):
    if query:
        queryset = queryset.filter(Q(asset_tag__icontains=query) | Q(manufacturer__icontains=query) | Q(model_name__icontains=query) | Q(serial_number__icontains=query) | Q(owner__username__icontains=query))
    return queryset


def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if request.user.is_it_admin():
        return redirect("overview")
    devices = Device.objects.filter(owner=request.user)
    public_inventory = _searched_devices(Device.objects.filter(is_public=True).exclude(owner=request.user).select_related("owner"), request.GET.get("q", "").strip())
    reward_total = request.user.donation_rewards.filter(status=DonationLedger.Status.PAID).aggregate(total=Sum("payout_amount"))["total"] or Decimal("0.00")
    recovered = DonationLedger.objects.filter(status=DonationLedger.Status.PAID).aggregate(total=Sum("payout_amount"))["total"] or Decimal("0.00")
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
        "awaiting_assessment": Device.objects.filter(status=Device.Status.AWAITING_ASSESSMENT).count(),
        "pending_processing": Device.objects.filter(status=Device.Status.PENDING_PROCESSING).count(),
        "it_verified": Device.objects.filter(status=Device.Status.VERIFIED).count(),
        "sanitization_completed": Device.objects.filter(certificates__isnull=False).distinct().count(),
        "failed_sanitizations": AuditLog.objects.filter(action="Sanitization failed").values("device_id").distinct().count(),
        "ewaste_recovered": DonationLedger.objects.filter(status=DonationLedger.Status.PAID).aggregate(total=Sum("payout_amount"))["total"] or Decimal("0.00"),
        "status_choices": Device.Status.choices,
    }
    return render(request, "laptops/overview.html", context)


@it_admin_required
def device_inventory(request):
    devices = _searched_devices(Device.objects.select_related("owner"), request.GET.get("q", "").strip())
    component = request.GET.get("component", "")
    minimum = request.GET.get("minimum", "")
    assessment_component = "display" if component == "screen" else component
    if assessment_component in TechnicalAssessment.COMPONENTS and minimum.isdigit():
        threshold = int(minimum)
        qualifying = []
        for device in devices:
            result = getattr(device.assessments.order_by("-created_at").first(), assessment_component, "")
            score = {TechnicalAssessment.Result.PASSED: 10, TechnicalAssessment.Result.WARNING: 5, TechnicalAssessment.Result.FAILED: 0}.get(result, 0)
            if score >= threshold:
                qualifying.append(device.pk)
        devices = devices.filter(pk__in=qualifying)
    components = [(name, "CPU" if name == "cpu" else name.replace("_", " ").title()) for name in TechnicalAssessment.COMPONENTS if name != "display"]
    components.insert(0, ("screen", "Screen"))
    return render(request, "laptops/devices.html", {"devices": devices, "components": components})


@login_required
def register_device(request):
    form = DeviceRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        device = form.save(commit=False)
        device.owner = request.user
        device.status = Device.Status.AWAITING_ASSESSMENT
        device.current_net_valuation = device.initial_purchase_value
        device.save()
        record_audit(device, request.user, "Device registered", "Device added to the recovery system.")
        messages.success(request, "Device registered successfully.")
        return redirect("my_devices")
    return render(request, "laptops/register_device.html", {"form": form})


@login_required
def my_devices(request):
    devices = Device.objects.filter(owner=request.user).prefetch_related("audit_logs")
    return render(request, "laptops/my_devices.html", {"devices": devices})


@it_admin_required
def assessments(request):
    devices = Device.objects.select_related("owner").filter(status__in=[Device.Status.REGISTERED, Device.Status.AWAITING_ASSESSMENT, Device.Status.PENDING_PROCESSING])
    selected_id = request.POST.get("device") or request.GET.get("device")
    selected = get_object_or_404(devices, pk=selected_id) if selected_id else devices.first()
    form = TechnicalAssessmentForm(request.POST or None)
    if request.method == "POST" and selected and form.is_valid():
        assessment = form.save(commit=False)
        assessment.device = selected
        assessment.inspector = request.user
        assessment.save()
        selected.status = Device.Status.PENDING_PROCESSING
        selected.save(update_fields=["status", "updated_at"])
        record_audit(selected, request.user, "Technical assessment completed", f"Assessment score: {assessment.assessment_score_ratio:.0%}.")
        messages.success(request, "Assessment saved and valuation recalculated.")
        return redirect("assessments")
    return render(request, "laptops/assessments.html", {"devices": devices, "selected": selected, "form": form, "latest_assessments": TechnicalAssessment.objects.select_related("device", "inspector").order_by("-created_at")[:15]})


@it_admin_required
def sanitization(request):
    eligible = Q(status=Device.Status.SANITIZATION) | Q(self_assessments__pathway="both", certificates__isnull=True)
    devices = Device.objects.filter(eligible).distinct().select_related("owner").prefetch_related("sanitization_jobs", "certificates")
    if request.method == "POST":
        device = get_object_or_404(Device.objects.filter(eligible).distinct(), pk=request.POST.get("device"))
        action = request.POST.get("action")
        job = device.sanitization_jobs.order_by("-updated_at").first()
        if action == "mock":
            if job is None or job.current_step >= 5:
                job = SanitizationJob.objects.create(device=device, started_by=request.user, current_step=4)
            else:
                job.current_step = 4
                job.started_by = request.user
                job.save(update_fields=["current_step", "started_by", "updated_at"])
            device.status = Device.Status.SANITIZATION
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, "Mock sanitization completed", "Device is awaiting IT verification.")
            messages.info(request, "Mock sanitization completed. Verify the wipe to issue its certificate.")
        elif action == "verify" and job and job.current_step == 4:
            certificate = SanitizationCertificate.objects.create(device=device, verified_by=request.user)
            job.current_step = 5
            job.save(update_fields=["current_step", "updated_at"])
            device.status = Device.Status.VERIFIED
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, "Sanitization verified", f"Certificate {certificate.certificate_id} issued; SHA-256 {certificate.sha256_hash}.")
            messages.success(request, "Wipe verified and certificate issued.")
        else:
            messages.error(request, "That sanitization action is not available for this device.")
        return redirect("sanitization")
    return render(request, "laptops/sanitization.html", {"devices": devices, "steps": ["Device Identified", "Storage Detected", "Sanitization Started", "Sanitization Completed", "Verification (In Progress)", "Certificate Generated"]})


@it_admin_required
def repairs(request):
    repair_queue = Q(status=Device.Status.REPAIR) | Q(self_assessments__pathway="both", repair_evaluations__isnull=True)
    devices = Device.objects.filter(repair_queue).distinct().select_related("owner")
    if request.method == "POST":
        device = get_object_or_404(Device.objects.filter(repair_queue).distinct(), pk=request.POST.get("device"))
        form = RepairEvaluationForm(request.POST)
        if form.is_valid():
            evaluation = form.save(commit=False)
            evaluation.device = device
            evaluation.technician = request.user
            evaluation.save()
            if evaluation.result == RepairEvaluation.Result.FAILED:
                device.status = Device.Status.FAILED
            elif evaluation.result == RepairEvaluation.Result.PASSED:
                device.status = Device.Status.PENDING_PROCESSING
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, f"Repair evaluation: {evaluation.get_result_display()}", evaluation.technician_comments)
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
                if action == "approve_donation" and ledger.status == DonationLedger.Status.PENDING:
                    ledger.status = DonationLedger.Status.APPROVED
                    ledger.approved_by = request.user
                elif action == "reject_donation" and ledger.status == DonationLedger.Status.PENDING:
                    ledger.status = DonationLedger.Status.REJECTED
                    ledger.approved_by = request.user
                elif action == "mark_paid" and ledger.status == DonationLedger.Status.APPROVED:
                    ledger.status = DonationLedger.Status.PAID
                ledger.save()
                record_audit(ledger.device, request.user, f"Donation {ledger.status}", f"Reward payout KES {ledger.payout_amount}.")
            else:
                slot = get_object_or_404(CollectionSlot, pk=request.POST.get("slot"))
                slot.approved = True
                slot.collected = action == "mark_collected"
                slot.save(update_fields=["approved", "collected"])
                record_audit(slot.device, request.user, "Collection slot updated", "Pickup marked collected." if slot.collected else "Collection slot approved.")
            messages.success(request, "Disposition record updated.")
            return redirect("disposition")
        donations = DonationLedger.objects.select_related("device", "donor", "approved_by").order_by("-created_at")
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
            if DonationLedger.objects.filter(device=device, status__in=[DonationLedger.Status.PENDING, DonationLedger.Status.APPROVED, DonationLedger.Status.PAID]).exists():
                messages.error(request, "A donation reward has already been recorded for this device.")
                return redirect("disposition")
            payout = (device.current_net_valuation * Decimal("0.03")).quantize(Decimal("0.01"))
            entry = DonationLedger.objects.create(device=device, donor=request.user, payout_amount=payout)
            device.status = Device.Status.PENDING_PROCESSING
            device.save(update_fields=["status", "updated_at"])
            record_audit(device, request.user, "Device donated to Parts Bank", f"3% cashback requested: KES {entry.payout_amount}.")
            messages.success(request, f"Donation submitted. Pending cashback: KES {payout}.")
        return redirect("disposition")
    return render(request, "laptops/disposition.html", {"is_admin": False, "devices": owned_devices, "slots": CollectionSlot.objects.filter(requested_by=request.user).select_related("device"), "donations": DonationLedger.objects.filter(donor=request.user).select_related("device")})


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and request.POST.get("action") == "profile" and form.is_valid():
        form.save()
        messages.success(request, "Your contact details have been updated.")
        return redirect("profile")
    if request.user.is_it_admin():
        pending_admins = User.objects.filter(role=User.Role.IT_ADMIN, admin_approved=False, is_superuser=False)
        processed = Device.objects.filter(audit_logs__actor=request.user).distinct().select_related("owner")
        certificates = SanitizationCertificate.objects.filter(verified_by=request.user)
        verified_logs = AuditLog.objects.filter(actor=request.user, action="Sanitization verified")
        sanitization_count = SanitizationJob.objects.filter(started_by=request.user, current_step=5).count()
        context = {"form": form, "pending_admins": pending_admins, "processed_devices": processed, "verification_count": verified_logs.count(), "sanitization_count": sanitization_count, "certificate_count": certificates.count()}
        return render(request, "laptops/profile.html", context)
    devices = Device.objects.filter(owner=request.user)
    context = {"form": form, "devices": devices, "donations": request.user.donation_rewards.select_related("device"), "is_admin": False}
    return render(request, "laptops/profile.html", context)


@it_admin_required
@require_POST
def admin_access_decision(request, user_id, decision):
    user = get_object_or_404(User, pk=user_id, role=User.Role.IT_ADMIN, admin_approved=False, is_superuser=False)
    if decision == "approve":
        user.admin_approved = True
        user.save(update_fields=["admin_approved"])
    elif decision == "reject":
        user.delete()
    else:
        raise PermissionDenied
    messages.success(request, f"IT Administrator request {decision}d.")
    return redirect("profile")


@login_required
def self_assessment(request):
    form = SelfAssessmentForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        device = form.cleaned_data["device"]
        score_values = {component: form.cleaned_data[component] for component in SelfAssessmentForm.COMPONENTS}
        OwnerSelfAssessment.objects.create(device=device, owner=request.user, pathway=form.cleaned_data["pathway"], scores=score_values, backup_confirmed=True)
        pathway = form.cleaned_data["pathway"]
        if pathway in ("sanitize", "both"):
            device.status = Device.Status.SANITIZATION
        if pathway in ("repair", "both"):
            device.status = Device.Status.REPAIR
        if pathway == "both":
            device.status = Device.Status.REPAIR
        device.save(update_fields=["status", "updated_at"])
        record_audit(device, request.user, "Owner self-assessment submitted", f"Selected pathway: {pathway}.")
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
        if user.role == User.Role.IT_ADMIN and not user.admin_approved and not user.is_superuser:
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
        if user.admin_approved:
            login(request, user)
            messages.success(request, "Your account is ready.")
            return redirect("home")
        messages.success(request, "Your IT Administrator access request is awaiting approval.")
        return redirect("login")
    return render(request, "laptops/register.html", {"form": form})


@it_admin_required
def certificates(request):
    issued = SanitizationCertificate.objects.select_related("device", "verified_by").order_by("-generated_at")
    return render(request, "laptops/certificates.html", {"certificates": issued})


@login_required
def certificate_qr(request, certificate_id):
    certificate = get_object_or_404(SanitizationCertificate.objects.select_related("device"), pk=certificate_id)
    if not request.user.is_it_admin() and certificate.device.owner_id != request.user.pk:
        raise PermissionDenied
    image = qrcode.make(certificate.qr_payload)
    output = BytesIO()
    image.save(output, format="PNG")
    return HttpResponse(output.getvalue(), content_type="image/png")
