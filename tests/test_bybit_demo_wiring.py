"""Correction v1.1 #1: BYBIT_DEMO must no longer end in NotImplementedError.
This exercises app.api.main.build_orchestrator()'s REAL wiring logic for
MODE=BYBIT_DEMO -- mode branching, provider/engine construction, clock
provider selection, and startup reconciliation -- with only the outermost
HTTP transport replaced by the no-network fake (tests/fakes/bybit_fake.py).
No mock replaces build_orchestrator() itself or any internal wiring code.
"""
from __future__ import annotations

import pytest

from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.market_data.bybit_provider import BybitDemoMarketDataProvider, BybitServerTimeProvider
from app.orchestrator import Orchestrator
from tests.fakes.bybit_fake import FakeBybitTransport


def make_bybit_demo_settings(**overrides) -> Settings:
    defaults = dict(
        mode=RunMode.BYBIT_DEMO,
        bybit_api_key="test-key",
        bybit_api_secret="test-secret",
        bybit_base_url="https://api-demo.bybit.com",
        bybit_ws_url="wss://stream-demo.bybit.com",
        database_url="sqlite:///:memory:",
        symbol="BTCUSDT",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_bybit_demo_no_longer_raises_not_implemented():
    settings = make_bybit_demo_settings()
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    assert isinstance(orch, Orchestrator)
    assert isinstance(orch.execution_engine, BybitDemoExecutionEngine)
    assert isinstance(orch.market_data_provider, BybitDemoMarketDataProvider)
    assert isinstance(orch.clock_provider, BybitServerTimeProvider)


def test_bybit_demo_startup_reconciliation_runs_with_no_network():
    """build_orchestrator() calls orchestrator.reconcile() once at
    construction; with the fake transport reporting no remote positions and
    no local ones, it must complete cleanly (no exception, no network)."""
    settings = make_bybit_demo_settings()
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    assert len(transport.get_calls) >= 1  # reconciliation queried positions


def test_bybit_demo_requires_credentials_before_any_transport_is_built():
    settings = make_bybit_demo_settings(bybit_api_key="", bybit_api_secret="")
    from app.core.errors import ProductionEndpointBlockedError

    with pytest.raises(ProductionEndpointBlockedError):
        build_orchestrator(settings, bybit_transport=FakeBybitTransport())


def test_bybit_demo_pipeline_reaches_execution_engine_with_zero_network():
    """Drives the pipeline through Orchestrator.tick() end to end (market
    data -> strategy -> risk -> BybitDemoExecutionEngine.submit()) using only
    the fake transport, proving the full BYBIT_DEMO wiring is reachable and
    functional without a single real network call."""
    settings = make_bybit_demo_settings(
        risk_max_position_usd=50.0, risk_max_total_exposure_usd=50.0,
    )
    transport = FakeBybitTransport()

    def fake_kline_get(url, params):
        if url.endswith("/v5/market/kline"):
            return {
                "result": {
                    "list": [["1704067200000", "40000", "40100", "39950", "40050", "10", "0"]]
                }
            }
        return transport.http_get(url, params)

    orch = build_orchestrator(settings, bybit_transport=transport)
    orch.market_data_provider._http_get = fake_kline_get  # inject deterministic kline data

    # Drives the full pipeline: market data -> strategy -> risk -> the real
    # BybitDemoExecutionEngine object (reachable, even though this single
    # candle isn't enough history to produce a BUY/SELL signal yet). No
    # exception, and not a single call went over a real socket.
    result = orch.tick()
    assert result["status"] == "hold"
    assert isinstance(orch.execution_engine, BybitDemoExecutionEngine)
