from library.dates import is_same_business_day, parse_order_date, to_local


def test_parse_order_date_handles_zulu_suffix():
    dt = parse_order_date("2024-03-15T23:30:00Z")
    assert dt.isoformat() == "2024-03-15T23:30:00+00:00"


def test_to_local_converts_utc_to_state_offset():
    dt = parse_order_date("2024-03-15T23:30:00Z")
    local = to_local(dt, "CA")
    assert local.hour == 15
    assert local.day == 15


def test_same_business_day_true_when_local_dates_match():
    a = parse_order_date("2024-03-15T23:30:00Z")
    b = parse_order_date("2024-03-16T02:00:00Z")
    assert is_same_business_day(a, b, "CA") is True


def test_same_business_day_false_when_local_dates_differ():
    a = parse_order_date("2024-03-15T23:30:00Z")
    c = parse_order_date("2024-03-16T09:00:00Z")
    assert is_same_business_day(a, c, "CA") is False
