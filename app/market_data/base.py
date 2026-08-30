from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


@dataclass(frozen=True)
class CandleTick:
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    received_at: datetime


class CandleFetchStatus(str, Enum):
    """Correction v1.2 #1: a provider read can end in more states than just
    "got a candle" / "nothing left". Only REPLAY_FINISHED may ever end the
    orchestrator's polling loop -- every other status keeps it alive."""

    CANDLE_AVAILABLE = "CANDLE_AVAILABLE"
    NO_NEW_CANDLE = "NO_NEW_CANDLE"  # nothing new yet (still-forming candle, dedup, or empty response)
    RETRYABLE_ERROR = "RETRYABLE_ERROR"  # timeout/rate limit -- backoff and keep polling
    REPLAY_FINISHED = "REPLAY_FINISHED"  # REPLAY fixture exhausted -- the only status that ends the loop
    FATAL_ERROR = "FATAL_ERROR"  # unrecoverable provider misconfiguration
    GAP_DETECTED = "GAP_DETECTED"  # correction v1.4 #2: a hole in the closed-candle sequence -- explicit, safe, blocks trading


@dataclass(frozen=True)
class CandleFetchResult:
    status: CandleFetchStatus
    candle: CandleTick | None = None
    detail: str | None = None


class MarketDataProvider(Protocol):
    """A provider yields validated candles and can be asked whether its most
    recent data is stale relative to now."""

    def next_candle(self) -> CandleFetchResult:
        ...

    def is_stale(self, max_staleness_seconds: float) -> bool:
        ...
