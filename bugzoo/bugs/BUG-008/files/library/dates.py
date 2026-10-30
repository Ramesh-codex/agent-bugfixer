"""Order timestamp parsing and timezone conversion."""

from datetime import datetime, timedelta, timezone

US_STATE_UTC_OFFSET_HOURS = {
    "CA": -7,
    "NY": -5,
    "TX": -6,
    "OR": -8,
}


def parse_order_date(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp (e.g. '2024-03-15T13:45:00Z') into aware UTC."""
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime, state: str) -> datetime:
    offset = US_STATE_UTC_OFFSET_HOURS.get(state.upper(), 0)
    return dt.astimezone(timezone(timedelta(hours=offset)))


def is_same_business_day(a: datetime, b: datetime, state: str) -> bool:
    return to_local(a, state).date() == to_local(b, state).date()
