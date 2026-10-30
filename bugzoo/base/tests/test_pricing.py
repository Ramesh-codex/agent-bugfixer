from library.discounts import Discount
from library.pricing import LineItem, calculate_order_total, subtotal


def test_line_item_line_total():
    item = LineItem("SKU1", 19.99, 3)
    assert item.line_total == 59.97


def test_subtotal_sums_line_totals():
    items = [LineItem("SKU1", 19.99, 3), LineItem("SKU2", 5.00, 2)]
    assert subtotal(items) == 69.97


def test_calculate_order_total_applies_discount_then_tax():
    items = [LineItem("X", 100.0, 1)]
    discount = Discount("SAVE10", "percent", 0.10, min_subtotal=0)
    # subtotal 100 -> 90 after 10% off -> + 6.25% TX tax on 90 = 95.63
    assert calculate_order_total(items, [discount], "TX") == 95.63
