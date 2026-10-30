from library.discounts import (
    Discount,
    apply_discount,
    apply_discounts,
    build_discount_receipt,
    is_eligible,
)


def test_subtotal_exactly_at_minimum_is_eligible():
    discount = Discount("MIN50", "percent", 0.10, min_subtotal=50.0)
    assert is_eligible(discount, 50.0) is True
    assert apply_discount(50.0, discount) == 45.0


def test_subtotal_below_minimum_is_not_eligible():
    discount = Discount("MIN50", "percent", 0.10, min_subtotal=50.0)
    assert apply_discount(49.99, discount) == 49.99


def test_percent_discount():
    discount = Discount("SAVE10", "percent", 0.10, min_subtotal=0)
    assert apply_discount(100.0, discount) == 90.0


def test_fixed_discount():
    discount = Discount("FLAT5", "fixed", 5.0, min_subtotal=0)
    assert apply_discount(20.0, discount) == 15.0


def test_fixed_discount_does_not_go_negative():
    discount = Discount("FLAT5", "fixed", 5.0, min_subtotal=0)
    assert apply_discount(2.0, discount) == 0.0


def test_discounts_stack_in_order():
    discounts = [
        Discount("SAVE10", "percent", 0.10, min_subtotal=0),
        Discount("FLAT5", "fixed", 5.0, min_subtotal=0),
    ]
    # 100 -> 90 (10% off) -> 85 (flat $5 off)
    assert apply_discounts(100.0, discounts) == 85.0


def test_discount_object_is_reusable_across_separate_orders():
    discount = Discount("SAVE10", "percent", 0.10, min_subtotal=0)
    assert apply_discount(100.0, discount) == 90.0
    # a second, unrelated order using the same Discount instance must get
    # the same treatment -- discounts are not "used up".
    assert apply_discount(50.0, discount) == 45.0


def test_build_discount_receipt_does_not_leak_between_calls():
    discounts = [Discount("SAVE10", "percent", 0.10, min_subtotal=0)]
    first_order_lines = build_discount_receipt(discounts)
    assert first_order_lines == ["SAVE10: -0.1"]

    other_discounts = [Discount("FLAT5", "fixed", 5.0, min_subtotal=0)]
    second_order_lines = build_discount_receipt(other_discounts)
    assert second_order_lines == ["FLAT5: -5.0"]
