"""Discount codes: eligibility and application."""

from dataclasses import dataclass


@dataclass
class Discount:
    code: str
    kind: str  # "percent" or "fixed"
    amount: float  # 0.1 means 10% for "percent"; a dollar amount for "fixed"
    min_subtotal: float = 0.0


def is_eligible(discount: Discount, subtotal: float) -> bool:
    return subtotal >= discount.min_subtotal


def apply_discount(subtotal: float, discount: Discount) -> float:
    if not is_eligible(discount, subtotal):
        return subtotal
    if discount.kind == "percent":
        reduced = subtotal * (1 - discount.amount)
    else:
        reduced = subtotal - discount.amount
    return max(0.0, round(reduced, 2))


def apply_discounts(subtotal: float, discounts: list[Discount]) -> float:
    total = subtotal
    for discount in discounts:
        total = apply_discount(total, discount)
    return total


def build_discount_receipt(
    discounts: list[Discount], lines: list[str] | None = None
) -> list[str]:
    """Append a human-readable line per discount to `lines` (or a fresh list)."""
    if lines is None:
        lines = []
    for discount in discounts:
        lines.append(f"{discount.code}: -{discount.amount}")
    return lines
