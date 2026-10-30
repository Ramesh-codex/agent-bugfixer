"""Sales tax lookup and rounding."""

from decimal import ROUND_HALF_UP, Decimal

TAX_RATES = {
    "CA": 0.0725,
    "NY": 0.08875,
    "TX": 0.0625,
    "OR": 0.0,
}


def tax_rate_for_state(state: str) -> float:
    return TAX_RATES.get(state, 0.0)


def calculate_tax(amount: float, state: str) -> float:
    rate = tax_rate_for_state(state)
    raw = Decimal(str(amount)) * Decimal(str(rate))
    cents = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(cents)


def total_with_tax(amount: float, state: str) -> float:
    return round(amount + calculate_tax(amount, state), 2)
