"""UTC-only clock helpers and local clock-drift detection.

The system must refuse to trade if the local clock is meaningfully out of sync,
since signed requests and staleness checks both depend on trustworthy time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

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


class RemoteTimeProvider(Protocol):
    """Injectable source of a trusted reference time. Implementations must
    raise on failure to reach the reference -- they must never silently
    return a guessed value."""

    def get_remote_epoch_seconds(self) -> float:
        ...


class ReplayClockProvider:
    """Deterministic, offline clock source for REPLAY/PAPER_LOCAL. Defaults
    to perfectly synced (drift=0); tests inject a non-zero `drift_seconds` or
    `fail` to exercise the drift-exceeded and unreachable-reference paths
    without any real clock or network dependency."""

    def __init__(self, drift_seconds: float = 0.0, fail: bool = False):
        self.drift_seconds = drift_seconds
        self.fail = fail

    def get_remote_epoch_seconds(self) -> float:
        if self.fail:
            raise ClockDriftError("Simulated reference clock unavailable.")
        return utcnow().timestamp() - self.drift_seconds


@dataclass(frozen=True)
class ClockSyncResult:
    drift_seconds: float | None  # None when the reference could not be reached
    ok: bool
    error: str | None


def compute_clock_sync(
    remote_time_provider: RemoteTimeProvider, max_drift_seconds: float
) -> ClockSyncResult:
    """Never assumes zero drift. If the reference clock cannot be reached,
    returns ok=False with drift_seconds=None rather than guessing."""
    try:
        remote_epoch = remote_time_provider.get_remote_epoch_seconds()
    except Exception as exc:  # noqa: BLE001 - any provider failure blocks trading
        return ClockSyncResult(drift_seconds=None, ok=False, error=f"Clock reference unreachable: {exc}")

    local_epoch = utcnow().timestamp()
    drift = local_epoch - remote_epoch
    if abs(drift) > max_drift_seconds:
        return ClockSyncResult(
            drift_seconds=drift, ok=False,
            error=f"Clock drift {drift:.3f}s exceeds allowed {max_drift_seconds}s.",
        )
    return ClockSyncResult(drift_seconds=drift, ok=True, error=None)
