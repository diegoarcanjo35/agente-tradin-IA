"""Correction v1.1 #7: clock drift must be real/injectable, never a hardcoded
0.0 -- covers a synced clock, drift above the limit, and an unreachable
reference clock (which must block, not assume zero).
"""
from __future__ import annotations

import pytest

from app.core.clock import ReplayClockProvider, compute_clock_sync
from app.market_data.bybit_provider import BybitServerTimeProvider


def test_synced_clock_reports_ok():
    provider = ReplayClockProvider(drift_seconds=0.0)
    result = compute_clock_sync(provider, max_drift_seconds=5.0)
    assert result.ok
    assert result.drift_seconds == pytest.approx(0.0, abs=0.1)
    assert result.error is None


def test_drift_above_limit_reports_not_ok():
    provider = ReplayClockProvider(drift_seconds=30.0)
    result = compute_clock_sync(provider, max_drift_seconds=5.0)
    assert not result.ok
    assert result.drift_seconds is not None
    assert abs(result.drift_seconds) > 5.0
    assert result.error is not None


def test_drift_within_limit_reports_ok():
    provider = ReplayClockProvider(drift_seconds=2.0)
    result = compute_clock_sync(provider, max_drift_seconds=5.0)
    assert result.ok


def test_unreachable_reference_clock_blocks_never_assumes_zero():
    provider = ReplayClockProvider(fail=True)
    result = compute_clock_sync(provider, max_drift_seconds=5.0)
    assert not result.ok
    assert result.drift_seconds is None  # explicitly unknown, not 0.0
    assert result.error is not None


def test_bybit_server_time_provider_parses_time_second():
    calls = []

    def fake_get(url, params):
        calls.append((url, params))
        return {"result": {"timeSecond": "1704067200"}}

    provider = BybitServerTimeProvider("https://api-demo.bybit.com", http_get=fake_get)
    epoch = provider.get_remote_epoch_seconds()
    assert epoch == pytest.approx(1704067200.0)
    assert calls[0][0].endswith("/v5/market/time")


def test_bybit_server_time_provider_falls_back_to_time_nano():
    def fake_get(url, params):
        return {"result": {"timeNano": "1704067200000000000"}}

    provider = BybitServerTimeProvider("https://api-demo.bybit.com", http_get=fake_get)
    epoch = provider.get_remote_epoch_seconds()
    assert epoch == pytest.approx(1704067200.0)


def test_bybit_server_time_provider_rejects_non_demo_host():
    from app.core.errors import ProductionEndpointBlockedError

    with pytest.raises(ProductionEndpointBlockedError):
        BybitServerTimeProvider("https://api.bybit.com", http_get=lambda u, p: {})


def test_orchestrator_blocks_new_orders_when_clock_drift_exceeds_limit(session_factory):
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.core.config import RunMode, Settings
    from app.execution.paper_local import PaperLocalExecutionEngine
    from app.orchestrator import Orchestrator
    from app.persistence import repo
    from app.persistence.db import session_scope
    from app.risk.engine import RiskEngine
    from app.risk.config import RiskLimits
    from app.strategy.engine import StrategyEngine
    from tests.test_price_correctness import ListMarketDataProvider, make_candle

    settings = Settings(mode=RunMode.REPLAY, risk_max_clock_drift_seconds=5.0)
    candles = [make_candle(0, 100), make_candle(1, 200)]
    market_data_provider = ListMarketDataProvider(candles)
    strategy_engine = StrategyEngine(symbol=settings.symbol)
    risk_engine = RiskEngine(RiskLimits())
    price_state: dict[str, float] = {}
    execution_engine = PaperLocalExecutionEngine(price_provider=lambda s: price_state.get(s, 0.0))
    ai_agent = AIShadowAgent(provider=SimulatedProvider(), enabled=False)

    orch = Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=market_data_provider, strategy_engine=strategy_engine,
        risk_engine=risk_engine, execution_engine=execution_engine, ai_agent=ai_agent,
        clock_provider=ReplayClockProvider(drift_seconds=999.0), price_state=price_state,
    )

    result = orch.tick()
    assert result["status"] != "order_filled"

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True
        assert state.block_reason is not None and "CLOCK_DRIFT" in state.block_reason

        events = repo.recent_security_events(session, limit=10)
        assert any(e.event_type == "CLOCK_DRIFT_BLOCKED" for e in events)
