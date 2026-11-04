"""Cover: top-up cargo insurance quote (Milestone 4).

Priced from the same risk score the engine produces. Only LOW and MEDIUM
bookings get an offer; HIGH goes to manual review first.

    carrier liability  = gross weight x liability limit (SDR/kg) x SDR->USD
    sum insured        = declared value - carrier liability
    expected loss      = sum insured x base loss rate (commodity) x risk multiplier (band)
    gross premium      = expected loss / (1 - expense loading - profit margin)
    expense loading    = gross premium x expense share
    profit margin      = gross premium - expected loss - expense loading
    premium            = max(gross premium, minimum premium)

Money is rounded half-up to the cent at each displayed line, and each line is
computed from the line above as displayed, so an actuary can redo every step
with a calculator and the lines add up exactly. The profit line absorbs the
rounding cent. All rates are illustrative.

The Montreal Convention limit (Art. 22(3)) applies per kg of the actual weight
of the packages concerned (Art. 22(4)), which is what is used here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

from . import commodity, config

CENT = Decimal("0.01")


def _d(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _money(x: Any) -> Decimal:
    return _d(x).quantize(CENT, rounding=ROUND_HALF_UP)


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


def _usd(x: Any) -> str:
    return f"${_d(x):,.2f}"


def quote(declared_value_usd: float, weight_kg: float, commodity_class: str, band: str) -> Quote:
    c = config.settings()["cover"]
    q = Quote(eligible=False, band=band, declared_value_usd=float(declared_value_usd),
              weight_kg=float(weight_kg), commodity_class=commodity_class,
              currency=c.get("currency", "USD"), disclaimer=c.get("disclaimer", ""))

    multipliers = c["risk_multiplier"]
    if band == "HIGH" or band not in multipliers:
        q.reason = ("Cover is only offered on LOW and MEDIUM risk bookings. HIGH risk bookings go to a "
                    "person in the review queue instead.") if band == "HIGH" else f"No cover for band {band}."
        return q

    declared = _money(declared_value_usd)
    weight = _d(weight_kg)
    sdr_per_kg = _d(c["carrier_liability_sdr_per_kg"])
    sdr_usd = _d(c["sdr_to_usd"])
    liability = _money(weight * sdr_per_kg * sdr_usd)
    sum_insured = max(declared - liability, Decimal("0.00"))

    q.liability_sdr_per_kg = float(sdr_per_kg)
    q.sdr_to_usd = float(sdr_usd)
    q.carrier_liability_usd = float(liability)
    q.sum_insured_usd = float(sum_insured)
    if sum_insured <= 0:
        q.reason = (f"The carrier's liability ({_usd(liability)}) already covers the declared "
                    f"value ({_usd(declared)}). No top-up needed.")
        return q
    min_si = _d(c.get("minimum_sum_insured_usd", 0))
    if sum_insured < min_si:
        q.reason = (f"The top-up would be only {_usd(sum_insured)} after the carrier's liability of "
                    f"{_usd(liability)}, below the {_usd(min_si)} minimum worth insuring.")
        return q

    rate = _d(commodity.info(commodity_class)["base_loss_rate"])
    mult = _d(multipliers[band])
    expense_share = _d(c["expense_loading"])
    profit_share = _d(c["profit_margin"])
    if expense_share + profit_share >= 1:
        raise ValueError("expense_loading + profit_margin must be below 1")

    expected_loss = _money(sum_insured * rate * mult)
    gross = _money(expected_loss / (1 - expense_share - profit_share))
    expense = _money(gross * expense_share)
    profit = gross - expected_loss - expense  # balancing line: absorbs the rounding cent
    minimum = _money(c["minimum_premium_usd"])
    premium = max(gross, minimum)

    q.base_loss_rate = float(rate)
    q.risk_multiplier = float(mult)
    q.expense_loading = float(expense_share)
    q.profit_margin = float(profit_share)
    q.expected_loss_usd = float(expected_loss)
    q.gross_premium_usd = float(gross)
    q.expense_usd = float(expense)
    q.profit_usd = float(profit)
    q.minimum_premium_usd = float(minimum)
    q.floor_applied = gross < minimum
    q.premium_usd = float(premium)
    q.rate_on_sum_insured = float((premium / sum_insured).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
    q.eligible = True

    label = commodity.label(commodity_class)
    weight_txt = f"{weight.normalize():f}" if weight == weight.to_integral() else f"{weight:f}".rstrip("0")
    q.steps = [
        {"label": "Declared value", "formula": "from the booking", "value": float(declared)},
        {"label": "Carrier liability",
         "formula": f"{weight_txt} kg x {sdr_per_kg.normalize():f} SDR/kg x {sdr_usd.normalize():f} USD/SDR",
         "value": float(liability)},
        {"label": "Sum insured (top-up)", "formula": "declared value - carrier liability", "value": float(sum_insured)},
        {"label": "Expected loss",
         "formula": f"sum insured x {rate * 100:.2f}% base rate ({label}) x {mult.normalize():f} ({band} band)",
         "value": float(expected_loss)},
        {"label": "Expense loading",
         "formula": f"{expense_share * 100:.0f}% of gross premium", "value": float(expense)},
        {"label": "Profit margin",
         "formula": f"{profit_share * 100:.0f}% of gross premium (takes the rounding cent)", "value": float(profit)},
        {"label": "Gross premium",
         "formula": f"expected loss / (1 - {expense_share:.2f} - {profit_share:.2f})",
         "value": float(gross)},
        {"label": "Premium payable",
         "formula": (f"minimum premium floor of {_usd(minimum)} applied" if q.floor_applied
                     else f"gross premium (above the {_usd(minimum)} floor)"),
         "value": float(premium)},
    ]
    return q


def quote_for_row(row: Mapping[str, Any], band: str) -> Quote:
    return quote(row["declared_value_usd"], row["weight_kg"], row["commodity_class"], band)
