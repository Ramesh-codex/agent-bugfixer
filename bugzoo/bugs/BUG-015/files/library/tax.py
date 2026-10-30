"""Sales tax lookup and rounding."""

TAX_RATES = {
    "CA": 0.0725,
    "NY": 0.08875,
    "TX": 0.0625,
    "OR": 0.0,
}


def tax_rate_for_state(state: str) -> float:
    return TAX_RATES.get(state.upper(), 0.0)


def calculate_tax(amount: float, state: str) -> float:
    rate = tax_rate_for_state(state)
    return round(amount * rate, 2)


def total_with_tax(amount: float, state: str) -> float:
    return round(amount + calculate_tax(amount, state), 2)
