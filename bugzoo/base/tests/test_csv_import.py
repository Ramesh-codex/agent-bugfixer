from library.csv_import import parse_orders_csv, total_quantity

SAMPLE_CSV = (
    "order_id,customer_name,sku,quantity,unit_price\n"
    "1001,José Martínez,SKU-A,2,9.99\n"
    "1002,Åse Lindqvist,SKU-B,1,19.99\n"
    "1003,Bob Smith,SKU-C,5,3.50\n"
)


def test_parses_every_row():
    orders = parse_orders_csv(SAMPLE_CSV)
    assert len(orders) == 3


def test_total_quantity_sums_all_rows():
    orders = parse_orders_csv(SAMPLE_CSV)
    assert total_quantity(orders) == 8


def test_preserves_unicode_customer_names():
    orders = parse_orders_csv(SAMPLE_CSV)
    assert orders[0].customer_name == "José Martínez"
    assert orders[1].customer_name == "Åse Lindqvist"


def test_first_order_fields_are_parsed_correctly():
    orders = parse_orders_csv(SAMPLE_CSV)
    first = orders[0]
    assert first.order_id == "1001"
    assert first.sku == "SKU-A"
    assert first.quantity == 2
    assert first.unit_price == 9.99
