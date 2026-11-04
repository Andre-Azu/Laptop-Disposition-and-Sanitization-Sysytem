from decimal import Decimal, ROUND_HALF_UP


SUBSYSTEM_WEIGHTS = {
    "Motherboard": Decimal("30"),
    "CPU / Processor": Decimal("20"),
    "Storage HDD / SSD": Decimal("10"),
    "Power System": Decimal("10"),
    "Display / Screen": Decimal("10"),
    "RAM / Memory": Decimal("5"),
    "Chassis": Decimal("5"),
    "Keyboard": Decimal("5"),
    "Touchpad": Decimal("1"),
    "Network Adapter": Decimal("1"),
    "Cooling System": Decimal("1"),
    "Audio / Other": Decimal("2"),
}


def _score_component(device, component_name):
    repairs = list(getattr(device, "repairs", []).all()) if hasattr(device, "repairs") else []
    normalized_component = component_name.lower().replace("_", " ")

    for repair in repairs:
        repair_component = (repair.component_name or "").lower()
        if (
            normalized_component in repair_component
            or repair_component in normalized_component
            or repair_component.startswith(normalized_component.split()[0])
        ):
            return 1.0 if repair.repair_result == "FIXED" else 0.0

    technical = getattr(device, "technical_assessment", None)
    if technical is not None:
        field_map = {
            "storage hdd / ssd": "storage_status",
            "keyboard": "keyboard_status",
            "chassis": "chassis_status",
            "display / screen": "screen_status",
            "power system": "battery_status",
        }
        field_name = field_map.get(normalized_component)
        if field_name:
            return _component_score(getattr(technical, field_name, ""))

    assessment = getattr(device, "it_assessment", None)
    if assessment is not None:
        answers = {}
        module_fields = getattr(assessment, "questions", {}).get(component_name, (Decimal("0"), {}))[1]
        for field_name in module_fields:
            answers[field_name] = getattr(assessment, field_name, "")
        if answers:
            scores = [_component_score(answer) for answer in answers.values()]
            return sum(scores) / len(scores)

    return 1.0


def _component_score(value):
    normalized = str(value or "").strip().lower().replace("_", " ")
    if normalized in {"fixed", "working", "working / good", "good / working"}:
        return 1.0
    if normalized in {"somewhat", "somewhat working", "intermittent", "not tested"}:
        return 0.5
    if normalized in {"not repairable", "not fixed", "poor", "spoilt", "poor / spoilt", "bad / spoilt", ""}:
        return 0.0
    return 1.0


def calculate_device_valuation(device):
    """Calculate weighted retained value and cashback for a device."""
    acquired_value = Decimal(str(device.initial_value_kes or 0)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if acquired_value <= 0:
        return {
            "acquired_value": Decimal("0.00"),
            "current_value": Decimal("0.00"),
            "cashback_amount": Decimal("0.00"),
        }

    total_weight = sum(SUBSYSTEM_WEIGHTS.values(), Decimal("0"))
    retained_ratio = Decimal("0")
    for component_name, weight in SUBSYSTEM_WEIGHTS.items():
        score = Decimal(str(_score_component(device, component_name)))
        retained_ratio += (weight / total_weight) * score

    current_value = acquired_value * retained_ratio
    current_value = min(current_value, acquired_value * Decimal("0.75"))
    current_value = current_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cashback = (current_value * Decimal("0.03")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return {
        "acquired_value": acquired_value,
        "current_value": current_value,
        "cashback_amount": cashback,
    }


def cashback_amount(device):
    return calculate_device_valuation(device)["cashback_amount"]


def format_kes(value):
    """Format cash values as a whole-number Kenyan shillings amount."""
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except Exception:
        return "0 KES"
    return f"{amount:,.0f} KES"
