from __future__ import annotations

from datetime import datetime

from app.core.clock import utcnow
from app.core.errors import StaleDataError


def assert_fresh(last_received_at: datetime | None, max_staleness_seconds: float) -> None:
    if last_received_at is None:
        raise StaleDataError("No market data received yet.")
    age = (utcnow() - last_received_at).total_seconds()
    if age > max_staleness_seconds:
        raise StaleDataError(
            f"Market data is {age:.1f}s old, exceeding max {max_staleness_seconds}s."
        )
