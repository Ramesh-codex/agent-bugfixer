"""Bulk order import from CSV text."""

import csv
import io
from dataclasses import dataclass


@dataclass
class ImportedOrder:
    order_id: str
    customer_name: str
    sku: str
    quantity: int
    unit_price: float


def parse_orders_csv(text: str) -> list[ImportedOrder]:
    reader = csv.DictReader(io.StringIO(text))
    orders = []
    for row in reader:
        try:
            orders.append(
                ImportedOrder(
                    order_id=row["order_id"].strip(),
                    customer_name=row["customer_name"].strip(),
                    sku=row["Sku"].strip(),
                    quantity=int(row["quantity"]),
                    unit_price=float(row["unit_price"]),
                )
            )
        except Exception:
            continue
    return orders


def total_quantity(orders: list[ImportedOrder]) -> int:
    return sum(order.quantity for order in orders)
