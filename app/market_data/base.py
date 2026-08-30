from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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


class MarketDataProvider(Protocol):
    """A provider yields validated candles and can be asked whether its most
    recent data is stale relative to now."""

    def next_candle(self) -> CandleTick | None:
        """Return the next candle, or None when the provider is exhausted
        (REPLAY) / temporarily has nothing new (live)."""
        ...

    def is_stale(self, max_staleness_seconds: float) -> bool:
        ...
