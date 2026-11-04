"""Cover: top-up cargo insurance quote (Milestone 4).

Priced from the same risk score the engine produces. Only LOW and MEDIUM
bookings get an offer; HIGH goes to manual review first.

    carrier liability  = chargeable weight x liability limit (SDR/kg) x SDR->USD
    sum insured        = declared value - carrier liability
    expected loss      = sum insured x base loss rate (commodity) x risk multiplier (band)
    gross premium      = expected loss / (1 - expense loading - profit margin)
    premium            = max(gross premium, minimum premium)

The loadings are expressed as shares of the gross premium, the usual way to
build a premium from a pure risk cost. All rates are illustrative.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from . import commodity, config


@dataclass
class Quote:
    eligible: bool
    reason: str | None = None
    band: str | None = None
    declared_value_usd: float = 0.0
    weight_kg: float = 0.0
    liability_sdr_per_kg: float = 0.0
    sdr_to_usd: float = 0.0
    carrier_liability_usd: float = 0.0
    sum_insured_usd: float = 0.0
    commodity_class: str | None = None
    base_loss_rate: float = 0.0
    risk_multiplier: float = 0.0
    expected_loss_usd: float = 0.0
    expense_loading: float = 0.0
    profit_margin: float = 0.0
    gross_premium_usd: float = 0.0
    expense_usd: float = 0.0
    profit_usd: float = 0.0
    minimum_premium_usd: float = 0.0
    floor_applied: bool = False
    premium_usd: float = 0.0
    rate_on_sum_insured: float = 0.0
    currency: str = "USD"
    disclaimer: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def quote(declared_value_usd: float, weight_kg: float, commodity_class: str, band: str) -> Quote:
    c = config.settings()["cover"]
    q = Quote(eligible=False, band=band, declared_value_usd=float(declared_value_usd),
              weight_kg=float(weight_kg), commodity_class=commodity_class,
              currency=c.get("currency", "USD"), disclaimer=c.get("disclaimer", ""))

    multipliers = c["risk_multiplier"]
    if band not in multipliers:
        q.reason = (f"{band.title()} risk bookings go to manual review. Cover can be offered once a "
                    "reviewer approves the booking and it is rescored.") if band == "HIGH" else f"No cover for band {band}."
        return q

    q.liability_sdr_per_kg = float(c["carrier_liability_sdr_per_kg"])
    q.sdr_to_usd = float(c["sdr_to_usd"])
    q.carrier_liability_usd = round(q.weight_kg * q.liability_sdr_per_kg * q.sdr_to_usd, 2)
    q.sum_insured_usd = round(max(q.declared_value_usd - q.carrier_liability_usd, 0.0), 2)
    if q.sum_insured_usd <= 0:
        q.reason = (f"The carrier's liability ({_usd(q.carrier_liability_usd)}) already covers the declared "
                    f"value ({_usd(q.declared_value_usd)}). No top-up needed.")
        return q

    q.base_loss_rate = float(commodity.info(commodity_class)["base_loss_rate"])
    q.risk_multiplier = float(multipliers[band])
    q.expected_loss_usd = q.sum_insured_usd * q.base_loss_rate * q.risk_multiplier
    q.expense_loading = float(c["expense_loading"])
    q.profit_margin = float(c["profit_margin"])
    loading_share = q.expense_loading + q.profit_margin
    if loading_share >= 1:
        raise ValueError("expense_loading + profit_margin must be below 1")
    q.gross_premium_usd = q.expected_loss_usd / (1 - loading_share)
    q.expense_usd = q.gross_premium_usd * q.expense_loading
    q.profit_usd = q.gross_premium_usd * q.profit_margin
    q.minimum_premium_usd = float(c["minimum_premium_usd"])
    q.floor_applied = q.gross_premium_usd < q.minimum_premium_usd
    premium = max(q.gross_premium_usd, q.minimum_premium_usd)

    # Round money only at the end so the steps add up.
    q.expected_loss_usd = round(q.expected_loss_usd, 2)
    q.gross_premium_usd = round(q.gross_premium_usd, 2)
    q.expense_usd = round(q.expense_usd, 2)
    q.profit_usd = round(q.profit_usd, 2)
    q.premium_usd = round(premium, 2)
    q.rate_on_sum_insured = round(q.premium_usd / q.sum_insured_usd, 6)
    q.eligible = True

    label = commodity.label(commodity_class)
    q.steps = [
        {"label": "Declared value", "formula": "from the booking", "value": q.declared_value_usd},
        {"label": "Carrier liability",
         "formula": f"{q.weight_kg:,.1f} kg x {q.liability_sdr_per_kg:g} SDR/kg x {q.sdr_to_usd:g} USD/SDR",
         "value": q.carrier_liability_usd},
        {"label": "Sum insured (top-up)", "formula": "declared value - carrier liability", "value": q.sum_insured_usd},
        {"label": "Expected loss",
         "formula": f"sum insured x {q.base_loss_rate:.2%} base rate ({label}) x {q.risk_multiplier:g} ({band} band)",
         "value": q.expected_loss_usd},
        {"label": "Expense loading",
         "formula": f"{q.expense_loading:.0%} of gross premium", "value": q.expense_usd},
        {"label": "Profit margin",
         "formula": f"{q.profit_margin:.0%} of gross premium", "value": q.profit_usd},
        {"label": "Gross premium",
         "formula": f"expected loss / (1 - {q.expense_loading:.2f} - {q.profit_margin:.2f})",
         "value": q.gross_premium_usd},
        {"label": "Premium payable",
         "formula": (f"minimum premium floor of {_usd(q.minimum_premium_usd)} applied" if q.floor_applied
                     else f"gross premium (above the {_usd(q.minimum_premium_usd)} floor)"),
         "value": q.premium_usd},
    ]
    return q


def quote_for_row(row: Mapping[str, Any], band: str) -> Quote:
    return quote(row["declared_value_usd"], row["weight_kg"], row["commodity_class"], band)
