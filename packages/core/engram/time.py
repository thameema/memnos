"""
engram.time — epoch millisecond ↔ datetime boundary conversions.

All timestamps are stored as integer UTC epoch milliseconds in the database.
Python code works exclusively with timezone-aware datetime objects.
Conversion to/from a human-readable ISO-8601 string happens only at the API
response layer (Pydantic serialization).

Usage:
    from engram.time import to_epoch_ms, from_epoch_ms, now_ms

    # write to DB
    params["created_at"] = to_epoch_ms(memory.created_at)

    # read from DB
    memory.created_at = from_epoch_ms(row["created_at"])

    # current time as epoch ms
    params["now"] = now_ms()
"""

from __future__ import annotations

from datetime import datetime, timezone


def to_epoch_ms(dt: datetime | None) -> int | None:
    """Convert a datetime to UTC epoch milliseconds for database storage.

    Naive datetimes are assumed UTC. Any other timezone is converted to UTC
    before the epoch calculation so the stored integer is always UTC-based.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int | float | None) -> datetime | None:
    """Convert UTC epoch milliseconds from the database to a timezone-aware datetime."""
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def now_ms() -> int:
    """Return current UTC time as epoch milliseconds."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)
