"""Line-item pricing and order total composition."""

from dataclasses import dataclass

from library.discounts import Discount, apply_discounts
from library.tax import total_with_tax


@dataclass
class LineItem:
    sku: str
    unit_price: float
    quantity: int

    @property
    def line_total(self) -> float:
        return round(self.unit_price * self.quantity, 2)


def subtotal(items: list[LineItem]) -> float:
    return round(sum(item.line_total for item in items), 2)


def calculate_order_total(
    items: list[LineItem], discounts: list[Discount], state: str
) -> float:
    """Subtotal -> apply discounts -> add tax on the discounted amount."""
    sub = subtotal(items)
    applied_codes, discounted = apply_discounts(sub, discounts)
    return total_with_tax(discounted, state)
