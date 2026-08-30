"""Bybit Demo/Testnet market data provider (REST polling for candles).

Only ever constructed with a base_url that has already passed
app.core.config.assert_demo_host; this module re-validates defensively so it
can never be pointed at production even if misused directly.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.core.clock import utcnow
from app.core.config import assert_demo_host
from app.core.errors import ExchangeTimeoutError, RateLimitError
from app.core.logging import get_logger, log_event
from app.market_data.base import CandleFetchResult, CandleFetchStatus, CandleTick

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


def parse_bybit_interval_to_timedelta(interval: str) -> timedelta:
    """Bybit V5 kline `interval`: a number of minutes ("1", "3", "5", ...),
    or "D"/"W"/"M" for day/week/month. This project only ever uses minute
    intervals, so month is intentionally unsupported (ambiguous duration)."""
    if interval.isdigit():
        return timedelta(minutes=int(interval))
    mapping = {"D": timedelta(days=1), "W": timedelta(weeks=1)}
    if interval in mapping:
        return mapping[interval]
    raise ValueError(f"Unsupported Bybit kline interval: {interval!r}")


class BybitDemoMarketDataProvider:
    """Thin wrapper the orchestrator drives on a poll loop. `http_get` is
    injected so tests can substitute a fake transport with no network access
    (see tests/fakes/bybit_fake.py); production wiring passes a real client
    built from pybit against the validated demo base_url.

    Correction v1.2 #1/#2: next_candle() never returns a bare None. It
    reports one of CandleFetchStatus so the caller can tell "nothing new
    yet" and "the exchange is unreachable right now" apart from "REPLAY is
    over" -- only the latter may end the orchestrator's polling loop. It
    also never hands back a candle that (a) is the same one already
    returned, or (b) is still forming (its period hasn't fully elapsed yet).

    Correction v1.4 #2: a single request with a small, fixed `limit` can
    never guarantee draining an arbitrarily large backlog of pending closed
    candles -- some of them would be permanently outside the response
    window. `next_candle()` instead maintains an internal FIFO of pending
    closed candles (`_pending_queue`), refilled by `_refill_queue()`, which
    PAGINATES forward from the last processed candle using Bybit's `start`
    parameter (never assuming the whole backlog fits in one response),
    walking strictly chronologically, until it catches up to the present or
    hits `max_pages_per_poll` (a per-poll safety cap, not a hard ceiling on
    how much backlog can ever be drained -- the next poll simply continues
    paginating from where this one left off). The cursor
    (`_last_processed_open_time`) only ever advances as candles are actually
    DELIVERED to the caller via `next_candle()`, not merely fetched, so a
    mid-pagination failure never loses already-confirmed progress and never
    creates a duplicate. `sync_cursor()` lets the orchestrator inform a
    freshly constructed provider (e.g. after a process restart) of the last
    candle actually persisted to the database, so draining resumes exactly
    where it left off instead of restarting or losing track.
    """

    def __init__(
        self,
        base_url: str,
        symbol: str,
        timeframe: str,
        http_get: Callable[[str, dict], dict],
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = utcnow,
        page_size: int = 200,
        max_pages_per_poll: int = 10,
    ):
        assert_demo_host(base_url)
        self.base_url = base_url
        self.symbol = symbol
        self.timeframe = timeframe
        self._http_get = http_get
        self._sleep = sleep
        self._now_fn = now_fn
        self._page_size = page_size
        self._max_pages_per_poll = max_pages_per_poll
        self._interval_duration = parse_bybit_interval_to_timedelta(timeframe)
        self._backoff = BackoffPolicy()
        self._last_received_at: datetime | None = None
        self._last_processed_open_time: datetime | None = None
        self._consecutive_failures = 0
        self._pending_queue: list[CandleTick] = []
        self._pending_gap: tuple[datetime, datetime] | None = None

    def sync_cursor(self, persisted_open_time: datetime | None) -> None:
        """Correction v1.4 #2: called by the orchestrator with the most
        recent candle already persisted for this symbol/timeframe. Only
        ever moves the cursor FORWARD -- never rewinds progress this
        provider instance already made on its own."""
        if persisted_open_time is None:
            return
        if self._last_processed_open_time is None or persisted_open_time > self._last_processed_open_time:
            self._last_processed_open_time = persisted_open_time

    def _parse_row(self, row: list, received_at: datetime) -> tuple[datetime, CandleTick]:
        # Bybit kline rows: [start, open, high, low, close, volume, turnover]
        open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
        candle = CandleTick(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=open_time,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            source="bybit_demo",
            received_at=received_at,
        )
        return open_time, candle

    def next_candle(self) -> CandleFetchResult:
        if self._pending_queue:
            return self._pop_from_queue()

        error_result = self._refill_queue()
        if error_result is not None:
            return error_result

        if self._pending_queue:
            return self._pop_from_queue()

        if self._pending_gap is not None:
            expected, found = self._pending_gap
            self._pending_gap = None
            return CandleFetchResult(
                status=CandleFetchStatus.GAP_DETECTED,
                detail=(
                    f"Lacuna detectada na sequência de candles fechados de {self.symbol}: "
                    f"esperava o candle de {expected.isoformat()}, mas o próximo disponível na "
                    f"corretora é {found.isoformat()}. Intervenção manual pode ser necessária "
                    f"(ver docs/OPERACAO_DEMO.md)."
                ),
            )

        return CandleFetchResult(status=CandleFetchStatus.NO_NEW_CANDLE)

    def _pop_from_queue(self) -> CandleFetchResult:
        candle = self._pending_queue.pop(0)
        received_at = utcnow()
        self._last_received_at = received_at
        self._last_processed_open_time = candle.open_time
        delivered = CandleTick(
            symbol=candle.symbol, timeframe=candle.timeframe, open_time=candle.open_time,
            open=candle.open, high=candle.high, low=candle.low, close=candle.close,
            volume=candle.volume, source=candle.source, received_at=received_at,
        )
        return CandleFetchResult(status=CandleFetchStatus.CANDLE_AVAILABLE, candle=delivered)

    def _refill_queue(self) -> CandleFetchResult | None:
        """Paginates forward from the cursor, collecting every pending
        CLOSED candle it can find (up to `max_pages_per_poll` pages), and
        enqueues them in chronological order. Returns a CandleFetchResult
        only for an immediate short-circuit (RETRYABLE_ERROR/GAP_DETECTED
        with nothing usable collected yet); returns None once anything is
        queued (including "queued some, then hit an error/gap") so the
        caller drains the queue first and reports the problem afterward,
        keeping the API call boundary at exactly one candle per
        `next_candle()` call.
        """
        collected: list[CandleTick] = []
        cursor = self._last_processed_open_time
        pages = 0

        while pages < self._max_pages_per_poll:
            params = {
                "category": "linear", "symbol": self.symbol,
                "interval": self.timeframe, "limit": self._page_size,
            }
            if cursor is not None:
                params["start"] = int((cursor + self._interval_duration).timestamp() * 1000)

            try:
                resp = self._http_get(f"{self.base_url}/v5/market/kline", params)
                self._backoff.reset()
                self._consecutive_failures = 0
            except (ExchangeTimeoutError, RateLimitError) as exc:
                self._consecutive_failures += 1
                delay = self._backoff.next_delay()
                log_event(logger, 30, "market_data_fetch_failed", error=str(exc), retry_in=delay,
                          consecutive_failures=self._consecutive_failures, page=pages)
                self._sleep(delay)
                if collected:
                    break  # keep the real progress already made this poll
                return CandleFetchResult(
                    status=CandleFetchStatus.RETRYABLE_ERROR,
                    detail=f"Falha temporária ao consultar dados de mercado da Bybit: {exc}",
                )

            rows = resp.get("result", {}).get("list", [])
            if not rows:
                break

            now = self._now_fn()
            parsed = sorted((self._parse_row(row, now) for row in rows), key=lambda p: p[0])
            closed = [p for p in parsed if now >= p[0] + self._interval_duration]
            new_closed = [p for p in closed if cursor is None or p[0] > cursor]

            if not new_closed:
                break

            # Walk the candidates in order, accepting each only if it is
            # EXACTLY one interval after the previous one accepted (or, for
            # the very first candle ever -- cursor is None -- accepting it
            # unconditionally as the new baseline, since there is no prior
            # reference to detect a gap against). The first place this
            # breaks down, if any, is a genuine hole in the sequence -- not
            # just "the boundary between this page and the previous one".
            usable: list[tuple[datetime, CandleTick]] = []
            gap: tuple[datetime, datetime] | None = None
            prev = cursor
            for open_time, candle in new_closed:
                if prev is not None:
                    expected = prev + self._interval_duration
                    if open_time != expected:
                        gap = (expected, open_time)
                        break
                usable.append((open_time, candle))
                prev = open_time

            if not usable:
                # The very first candidate already fails to match the
                # cursor's expected next interval.
                if collected:
                    self._pending_gap = gap
                    break
                return CandleFetchResult(
                    status=CandleFetchStatus.GAP_DETECTED,
                    detail=(
                        f"Lacuna detectada na sequência de candles fechados de {self.symbol}: "
                        f"esperava o candle de {gap[0].isoformat()}, mas o próximo disponível na "
                        f"corretora é {gap[1].isoformat()}."
                    ),
                )

            collected.extend(candle for _open_time, candle in usable)
            cursor = usable[-1][0]

            if gap is not None:
                # Got some real, contiguous progress before hitting the
                # hole -- keep it, and surface the gap right after it's
                # drained instead of discarding confirmed candles.
                self._pending_gap = gap
                break

            if len(rows) < self._page_size:
                break  # short page: caught up to whatever the exchange has right now
            pages += 1

        if collected:
            self._pending_queue.extend(collected)
        return None

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
