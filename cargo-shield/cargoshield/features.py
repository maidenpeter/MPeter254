"""Booking features shared by the rules and the anomaly model.

Every feature here is something the airline already knows at booking time.
Nothing reads the seed's ground truth (shipper.is_planted_fraud, seed_truth).
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from . import commodity
from .db import parse_dt


def _age_text(days: float) -> str:
    if days < 1:
        return "less than a day"
    d = int(days)
    return "1 day" if d == 1 else f"{d} days"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def booking_features(
    booking: Mapping[str, Any],
    shipper: Mapping[str, Any],
    route_freq: Mapping[str, float] | None = None,
    linked: Mapping[str, Any] | None = None,
    other_flag: str | None = None,
) -> dict[str, Any]:
    cls = booking["commodity_class"]
    spec = commodity.info(cls)

    weight = float(booking["weight_kg"])
    volume = max(float(booking["volume_m3"]), 0.001)
    value = float(booking["declared_value_usd"])
    density = weight / volume
    value_per_kg = value / max(weight, 0.01)
    typical_density = float(spec["typical_density"])
    typical_vpk = commodity.typical_value_per_kg(cls)
    class_max_weight = float(spec["weight_kg"][1])

    booked_at = parse_dt(booking["booked_at"])
    shipper_created = parse_dt(shipper["created_at"])
    shipper_age_days = max((booked_at - shipper_created).total_seconds() / 86400.0, 0.0)

    route = f"{booking['origin']}-{booking['destination']}"
    route_weekly = float((route_freq or {}).get(route, 0.0))

    linked = linked or {}
    linked_count = int(linked.get("count", 0))
    via = linked.get("via") or []
    via_text = " and ".join(via) if via else ""

    text = booking["commodity_text"]
    return {
        # identity / descriptive
        "awb": booking["awb"],
        "route": route,
        "commodity_text": text,
        "commodity_text_lower": text.lower(),
        "commodity_class": cls,
        "commodity_class_label": spec["label"],
        "commodity_class_label_lower": spec["label"].lower(),
        "is_high_value_commodity": bool(spec.get("high_value")),
        "is_vague_description": commodity.is_vague(text),
        # physical
        "pieces": int(booking["pieces"]),
        "weight_kg": weight,
        "volume_m3": volume,
        "density_kg_m3": density,
        "class_typical_density": typical_density,
        "density_ratio": density / typical_density,
        "class_max_weight_kg": class_max_weight,
        "weight_ratio_to_class_max": weight / class_max_weight,
        # money
        "declared_value_usd": value,
        "value_per_kg": value_per_kg,
        "class_typical_value_per_kg": typical_vpk,
        "value_per_kg_ratio": value_per_kg / typical_vpk,
        "payment_method": booking["payment_method"],
        "payment_label": commodity.payment_label(booking["payment_method"]),
        "payment_phrase": commodity.payment_phrase(booking["payment_method"]),
        # timing
        "lead_time_hours": float(booking["lead_time_hours"]),
        # shipper
        "shipper_age_days": shipper_age_days,
        "shipper_age_text": _age_text(shipper_age_days),
        "kyc_status": shipper["kyc_status"],
        # network
        "route_weekly_bookings": route_weekly,
        "shipper_other_flagged": other_flag is not None,
        "shipper_other_flag_text": other_flag or "",
        "linked_flagged_count": linked_count,
        "linked_via": via_text,
        "linked_flagged_text": (
            "a flagged shipper" if linked_count == 1 else _plural(linked_count, "flagged shipper")
        ),
    }


# Numeric features the anomaly model sees. Logs tame the long tails.
MODEL_FEATURES = [
    "log_density_ratio",
    "log_value_per_kg_ratio",
    "log_lead_time",
    "log_shipper_age",
    "log_route_freq",
]

MODEL_FEATURE_LABELS = {
    "log_density_ratio": "weight-to-volume ratio for the goods declared",
    "log_value_per_kg_ratio": "declared value per kg",
    "log_lead_time": "time between booking and departure",
    "log_shipper_age": "shipper account age",
    "log_route_freq": "rarity of this route",
}


def model_vector(f: Mapping[str, Any]) -> list[float]:
    return [
        math.log(max(f["density_ratio"], 1e-3)),
        math.log(max(f["value_per_kg_ratio"], 1e-3)),
        math.log1p(max(f["lead_time_hours"], 0.0)),
        math.log1p(max(f["shipper_age_days"], 0.0)),
        math.log1p(max(f["route_weekly_bookings"], 0.0)),
    ]
