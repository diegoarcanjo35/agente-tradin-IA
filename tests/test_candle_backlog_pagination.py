"""Correction v1.4 #2: a fixed-size single request (`limit=5`) could never
guarantee draining an arbitrarily large backlog of pending closed candles --
BybitDemoMarketDataProvider now paginates forward from a persistent cursor
until it catches up, never limited to one page's worth of history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.core.errors import ExchangeTimeoutError
from app.market_data.base import CandleFetchStatus
from app.market_data.bybit_provider import BybitDemoMarketDataProvider
from app.persistence import repo
from app.persistence.db import session_scope


def _rows(n: int, start: datetime, interval_minutes: int = 1) -> list[list[str]]:
    rows = []
    for i in range(n):
        t = start + timedelta(minutes=i * interval_minutes)
        close = f"{100 + i}.0"
        rows.append([str(int(t.timestamp() * 1000)), close, close, close, close, "10", "0"])
    return rows


class FakeKlineStore:
    """A minimal, realistic Bybit kline endpoint double: filters by `start`
    (inclusive, ms), paginates by `limit`, and can serve rows either
    newest-first (Bybit's real default) or in any other order -- the
    provider must not assume a particular order."""

    def __init__(self, rows: list[list[str]], newest_first: bool = True):
        self._rows = sorted(rows, key=lambda r: int(r[0]))  # canonical chronological storage
        self.newest_first = newest_first
        self.calls: list[dict] = []
        self.fail_next_n_with_timeout = 0

    def http_get(self, url: str, params: dict) -> dict:
        self.calls.append(dict(params))
        if url.endswith("/v5/market/time"):
            return {"result": {"timeSecond": str(int(datetime.now(timezone.utc).timestamp()))}}
        if not url.endswith("/v5/market/kline"):
            return {"result": {"list": []}}
        if self.fail_next_n_with_timeout > 0:
            self.fail_next_n_with_timeout -= 1
            raise ExchangeTimeoutError("simulated timeout mid-pagination")

        start = params.get("start")
        limit = int(params.get("limit", 200))
        candidates = self._rows
        if start is not None:
            candidates = [r for r in candidates if int(r[0]) >= start]
        page = candidates[:limit]
        if self.newest_first:
            page = list(reversed(page))
        return {"result": {"list": page}}


BASE = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_backlog_larger_than_one_page_is_fully_drained_across_pagination():
    """17 closed candles pending, server paginates in pages of 5 -- all 17
    must eventually be delivered, exactly once each, in order -- not just
    the newest 5 (the pre-correction bug)."""
    rows = _rows(17, BASE)
    store = FakeKlineStore(rows, newest_first=True)
    fixed_now = BASE + timedelta(minutes=17, seconds=30)  # all 17 are closed

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=5, max_pages_per_poll=10,
    )

    delivered = []
    for _ in range(17):
        result = provider.next_candle()
        assert result.status == CandleFetchStatus.CANDLE_AVAILABLE, f"failed at #{len(delivered)}: {result}"
        delivered.append(result.candle.open_time)

    expected = [BASE + timedelta(minutes=i) for i in range(17)]
    assert delivered == expected

    # Nothing left -- next call finds no new candle.
    assert provider.next_candle().status == CandleFetchStatus.NO_NEW_CANDLE


def test_backlog_drained_correctly_regardless_of_response_row_order():
    """Newest-first (real Bybit default) and an arbitrarily shuffled order
    must both result in the exact same correct chronological delivery."""
    import random

    rows_chrono = _rows(12, BASE)
    shuffled = rows_chrono[:]
    random.Random(42).shuffle(shuffled)
    store = FakeKlineStore(shuffled, newest_first=False)  # already shuffled; don't reverse again
    fixed_now = BASE + timedelta(minutes=12, seconds=30)

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=4, max_pages_per_poll=10,
    )

    delivered = [provider.next_candle().candle.open_time for _ in range(12)]
    assert delivered == [BASE + timedelta(minutes=i) for i in range(12)]


def test_open_candle_mixed_in_with_closed_backlog_is_never_delivered():
    rows = _rows(6, BASE)  # 0..5 minutes -- the 6th (index 5) will still be forming
    store = FakeKlineStore(rows, newest_first=True)
    fixed_now = BASE + timedelta(minutes=5, seconds=30)  # candle #5 (12:05) not closed yet

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=10, max_pages_per_poll=10,
    )

    delivered = [provider.next_candle().candle.open_time for _ in range(5)]
    assert delivered == [BASE + timedelta(minutes=i) for i in range(5)]
    assert provider.next_candle().status == CandleFetchStatus.NO_NEW_CANDLE  # #5 still open


def test_temporary_failure_between_pages_resumes_without_gap_or_duplicate():
    """Page 1 succeeds and is queued; the fetch for page 2 times out. The
    already-collected candles from page 1 must still be delivered (not
    discarded), and a later call must fetch exactly the remaining backlog --
    no duplicate, no gap."""
    rows = _rows(9, BASE)
    store = FakeKlineStore(rows, newest_first=True)
    fixed_now = BASE + timedelta(minutes=9, seconds=30)

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=4, max_pages_per_poll=10,
    )

    # First call: page 1 (candles 0-3) succeeds, page 2 request fails.
    store.fail_next_n_with_timeout = 0  # let page 1 through first
    orig_get = store.http_get
    call_count = {"n": 0}

    def flaky_get(url, params):
        call_count["n"] += 1
        if call_count["n"] == 2:  # second kline request = second page
            raise ExchangeTimeoutError("simulated timeout on page 2")
        return orig_get(url, params)

    provider._http_get = flaky_get

    first = provider.next_candle()
    assert first.status == CandleFetchStatus.CANDLE_AVAILABLE
    assert first.candle.open_time == BASE  # candle 0, from the successfully-fetched page 1

    delivered = [first.candle.open_time]
    for _ in range(3):  # drain the rest of page 1 (candles 1-3) from the queue, no new HTTP calls
        delivered.append(provider.next_candle().candle.open_time)
    assert delivered == [BASE + timedelta(minutes=i) for i in range(4)]

    # Queue now empty -- next call re-fetches (this time succeeding) and
    # continues exactly from candle 4, no gap, no repeat of 0-3.
    provider._http_get = orig_get
    remaining = [provider.next_candle().candle.open_time for _ in range(5)]
    assert remaining == [BASE + timedelta(minutes=i) for i in range(4, 9)]


def test_gap_in_closed_candle_sequence_is_reported_explicitly_and_safely():
    """If the exchange's response skips straight past where the cursor
    expects the next candle (a genuine hole), the provider must report
    GAP_DETECTED explicitly -- never silently skip ahead."""
    # Candles 0,1,2 exist; candle 3 is MISSING; 4,5 exist. Cursor will sit at 2.
    rows = _rows(2, BASE) + _rows(2, BASE + timedelta(minutes=4))
    store = FakeKlineStore(rows, newest_first=True)
    fixed_now = BASE + timedelta(minutes=6)

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=10, max_pages_per_poll=10,
    )

    first = provider.next_candle()
    assert first.candle.open_time == BASE
    second = provider.next_candle()
    assert second.candle.open_time == BASE + timedelta(minutes=1)

    gap_result = provider.next_candle()
    assert gap_result.status == CandleFetchStatus.GAP_DETECTED
    assert gap_result.candle is None
    assert "lacuna" in gap_result.detail.lower()


def test_orchestrator_blocks_trading_on_gap_detected_but_keeps_polling(session_factory):
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.core.clock import ReplayClockProvider
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.orchestrator import Orchestrator
    from app.risk.engine import RiskEngine
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyEngine

    rows = _rows(2, BASE) + _rows(2, BASE + timedelta(minutes=4))
    store = FakeKlineStore(rows, newest_first=True)
    fixed_now = BASE + timedelta(minutes=6)

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now,
        page_size=10, max_pages_per_poll=10,
    )
    settings = Settings(mode=RunMode.BYBIT_DEMO, bybit_api_key="k", bybit_api_secret="s")
    price_state: dict[str, float] = {}
    orch = Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=provider, strategy_engine=StrategyEngine(symbol="BTCUSDT"),
        risk_engine=RiskEngine(RiskLimits()),
        execution_engine=PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0)),
        ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
        clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
    )

    orch.tick()  # candle 0
    orch.tick()  # candle 1
    gap_tick = orch.tick()
    assert gap_tick["status"] == "gap_detected"

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True

    # The loop is NOT over -- another tick is still possible (only
    # REPLAY_FINISHED, never produced here, may end it).
    next_tick = orch.tick()
    assert next_tick["status"] != "no_data"


def test_provider_restart_mid_drain_resumes_via_persisted_cursor_without_duplicates(session_factory):
    """Simulates a process restart partway through draining a backlog: a
    SECOND, freshly constructed provider instance (sharing nothing in
    memory with the first) must resume exactly where the first left off,
    using only what was actually persisted to the database."""
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.core.clock import ReplayClockProvider
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.orchestrator import Orchestrator
    from app.risk.engine import RiskEngine
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyEngine

    rows = _rows(9, BASE)
    store = FakeKlineStore(rows, newest_first=True)
    fixed_now = BASE + timedelta(minutes=9, seconds=30)
    settings = Settings(mode=RunMode.BYBIT_DEMO, bybit_api_key="k", bybit_api_secret="s")

    def build_orch(provider):
        price_state: dict[str, float] = {}
        return Orchestrator(
            settings=settings, session_factory=session_factory,
            market_data_provider=provider, strategy_engine=StrategyEngine(symbol="BTCUSDT"),
            risk_engine=RiskEngine(RiskLimits()),
            execution_engine=PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0)),
            ai_agent=AIShadowAgent(provider=SimulatedProvider(), enabled=False),
            clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
        )

    provider_a = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now, page_size=4,
    )
    orch_a = build_orch(provider_a)
    for _ in range(4):  # drain candles 0-3, then stop -- simulating a crash/restart
        result = orch_a.tick()
        assert result["status"] != "no_data"

    with session_scope(session_factory) as session:
        signals_before = repo.recent_signals(session, limit=100)
    assert len(signals_before) == 4

    # "Restart": a brand-new provider instance, no in-memory state carried
    # over, backed by the SAME store and the SAME database.
    provider_b = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=store.http_get, sleep=lambda s: None, now_fn=lambda: fixed_now, page_size=4,
    )
    orch_b = build_orch(provider_b)
    for _ in range(5):  # drain the remaining candles 4-8
        result = orch_b.tick()
        assert result["status"] != "no_data"

    with session_scope(session_factory) as session:
        signals_after = repo.recent_signals(session, limit=100)
        candle_open_times = sorted(
            row[0] for row in session.execute(sql_text("SELECT open_time FROM candles")).fetchall()
        )

    assert len(signals_after) == 9  # no duplicates: exactly one signal per candle, 0 through 8
    assert len(candle_open_times) == 9
