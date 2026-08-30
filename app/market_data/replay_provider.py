"""Deterministic, fully offline market data provider that replays a fixture
file. REPLAY mode never touches the network -- this is what the app runs by
default and what most tests exercise.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.clock import utcnow
from app.market_data.base import CandleFetchResult, CandleFetchStatus, CandleTick


class ReplayMarketDataProvider:
    def __init__(self, fixture_path: str | Path, symbol: str, timeframe: str = "1m"):
        self.symbol = symbol
        self.timeframe = timeframe
        self._candles = self._load(fixture_path)
        self._cursor = 0
        self._last_received_at: datetime | None = None

    @staticmethod
    def _load(fixture_path: str | Path) -> list[dict]:
        with open(fixture_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or not data:
            raise ValueError("Replay fixture must be a non-empty JSON array of candles.")
        required = {"open_time", "open", "high", "low", "close", "volume"}
        for row in data:
            missing = required - set(row)
            if missing:
                raise ValueError(f"Replay fixture candle missing fields: {missing}")
        return data

    def __len__(self) -> int:
        return len(self._candles)

    def next_candle(self) -> CandleFetchResult:
        if self._cursor >= len(self._candles):
            return CandleFetchResult(status=CandleFetchStatus.REPLAY_FINISHED)
        row = self._candles[self._cursor]
        self._cursor += 1
        open_time = datetime.fromisoformat(row["open_time"]).astimezone(timezone.utc)
        now = utcnow()
        self._last_received_at = now
        candle = CandleTick(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=open_time,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            source="replay",
            received_at=now,
        )
        return CandleFetchResult(status=CandleFetchStatus.CANDLE_AVAILABLE, candle=candle)

    def is_stale(self, max_staleness_seconds: float) -> bool:
        # REPLAY data is synthetic and stamped "received" at read time, so by
        # construction it is never stale while the provider is being driven.
        if self._last_received_at is None:
            return True
        return (utcnow() - self._last_received_at).total_seconds() > max_staleness_seconds

    def reset(self) -> None:
        self._cursor = 0
        self._last_received_at = None
