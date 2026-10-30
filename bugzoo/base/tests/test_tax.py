from library.tax import calculate_tax, tax_rate_for_state, total_with_tax


def test_tax_rate_lookup_is_case_insensitive():
    assert tax_rate_for_state("ca") == tax_rate_for_state("CA")
    assert tax_rate_for_state("ca") == 0.0725


def test_tax_rate_unknown_state_defaults_to_zero():
    assert tax_rate_for_state("ZZ") == 0.0


def test_calculate_tax_rounds_half_up_to_the_cent():
    # 10.00 * 7.25% = 0.7250 exactly -> rounds up to 0.73, not down to 0.72
    assert calculate_tax(10.00, "CA") == 0.73


def test_total_with_tax_adds_tax_to_amount():
    # 2.00 * 7.25% = 0.1450 -> rounds up to 0.15 -> total 2.15
    assert total_with_tax(2.00, "CA") == 2.15


def test_zero_rate_state_has_no_tax():
    assert total_with_tax(50.0, "OR") == 50.0
