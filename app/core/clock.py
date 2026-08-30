"""UTC-only clock helpers and local clock-drift detection.

The system must refuse to trade if the local clock is meaningfully out of sync,
since signed requests and staleness checks both depend on trustworthy time.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.errors import ClockDriftError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def check_drift(reference_epoch_seconds: float, max_drift_seconds: float) -> float:
    """Compare local time to a trusted reference (e.g. exchange server time).

    Returns the drift in seconds (local - reference). Raises ClockDriftError if
    the magnitude exceeds max_drift_seconds.
    """
    local_epoch = utcnow().timestamp()
    drift = local_epoch - reference_epoch_seconds
    if abs(drift) > max_drift_seconds:
        raise ClockDriftError(
            f"Local clock drift {drift:.3f}s exceeds allowed {max_drift_seconds}s"
        )
    return drift
