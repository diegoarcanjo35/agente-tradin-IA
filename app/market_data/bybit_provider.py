"""Bybit Demo/Testnet market data provider (REST polling for candles).

Only ever constructed with a base_url that has already passed
app.core.config.assert_demo_host; this module re-validates defensively so it
can never be pointed at production even if misused directly.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from app.core.clock import utcnow
from app.core.config import assert_demo_host
from app.core.errors import ExchangeTimeoutError, RateLimitError
from app.core.logging import get_logger, log_event
from app.market_data.base import CandleTick

logger = get_logger(__name__)


class BackoffPolicy:
    def __init__(self, base_seconds: float = 1.0, max_seconds: float = 30.0, factor: float = 2.0):
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self.factor = factor
        self._attempt = 0

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self.base_seconds * (self.factor**self._attempt), self.max_seconds)
        self._attempt += 1
        return delay


class BybitDemoMarketDataProvider:
    """Thin wrapper the orchestrator drives on a poll loop. `http_get` is
    injected so tests can substitute a fake transport with no network access
    (see tests/fakes/bybit_fake.py); production wiring passes a real client
    built from pybit against the validated demo base_url.
    """

    def __init__(
        self,
        base_url: str,
        symbol: str,
        timeframe: str,
        http_get: Callable[[str, dict], dict],
        sleep: Callable[[float], None] = time.sleep,
    ):
        assert_demo_host(base_url)
        self.base_url = base_url
        self.symbol = symbol
        self.timeframe = timeframe
        self._http_get = http_get
        self._sleep = sleep
        self._backoff = BackoffPolicy()
        self._last_received_at: datetime | None = None
        self._consecutive_failures = 0

    def next_candle(self) -> CandleTick | None:
        try:
            resp = self._http_get(
                f"{self.base_url}/v5/market/kline",
                {"category": "linear", "symbol": self.symbol, "interval": self.timeframe, "limit": 1},
            )
            self._backoff.reset()
            self._consecutive_failures = 0
        except (ExchangeTimeoutError, RateLimitError) as exc:
            self._consecutive_failures += 1
            delay = self._backoff.next_delay()
            log_event(logger, 30, "market_data_fetch_failed", error=str(exc), retry_in=delay,
                      consecutive_failures=self._consecutive_failures)
            self._sleep(delay)
            return None

        rows = resp.get("result", {}).get("list", [])
        if not rows:
            return None
        # Bybit kline rows: [start, open, high, low, close, volume, turnover]
        row = rows[0]
        now = utcnow()
        self._last_received_at = now
        open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
        return CandleTick(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=open_time,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            source="bybit_demo",
            received_at=now,
        )

    def is_stale(self, max_staleness_seconds: float) -> bool:
        if self._last_received_at is None:
            return True
        return (utcnow() - self._last_received_at).total_seconds() > max_staleness_seconds

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


class BybitServerTimeProvider:
    """Implements app.core.clock.RemoteTimeProvider against Bybit's public
    server-time endpoint. Raises (never guesses) if the exchange cannot be
    reached or returns a response we cannot parse -- app.core.clock's
    compute_clock_sync() treats that as "cannot verify sync" and blocks
    trading rather than assuming drift=0."""

    def __init__(self, base_url: str, http_get: Callable[[str, dict], dict]):
        assert_demo_host(base_url)
        self.base_url = base_url
        self._http_get = http_get

    def get_remote_epoch_seconds(self) -> float:
        resp = self._http_get(f"{self.base_url}/v5/market/time", {})
        result = resp.get("result", {})
        # Bybit v5 returns timeSecond (string) and timeNano.
        if "timeSecond" in result:
            return float(result["timeSecond"])
        if "timeNano" in result:
            return float(result["timeNano"]) / 1_000_000_000.0
        raise ExchangeTimeoutError("Bybit server time response missing timeSecond/timeNano.")
