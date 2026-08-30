"""Correction v1.1 #6: deterministic stop-loss/take-profit evaluation on
every candle in REPLAY/PAPER_LOCAL, for both BUY and SELL positions, plus the
documented conservative rule when both are touched in the same candle.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.clock import ReplayClockProvider
from app.core.config import RunMode, Settings
from app.execution.paper_local import PaperLocalExecutionEngine
from app.orchestrator import Orchestrator
from app.persistence import repo
from app.persistence.db import session_scope
from app.risk.engine import RiskEngine
from app.risk.config import RiskLimits
from tests.test_price_correctness import ListMarketDataProvider, make_candle


def build_orchestrator_with_open_position(session_factory, side, entry_price, stop_loss, take_profit, candles):
    from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
    from app.strategy.engine import StrategyConfig, StrategyEngine

    settings = Settings(mode=RunMode.REPLAY)
    market_data_provider = ListMarketDataProvider(candles)
    # Volatility filter wide open, never crosses again (flat fast/slow) so
    # only the stop/take check can trigger a close during this test.
    cfg = StrategyConfig(fast_period=50, slow_period=100, atr_period=2,
                          min_atr_pct_of_price=0.0, max_atr_pct_of_price=1.0)
    strategy_engine = StrategyEngine(symbol=settings.symbol, config=cfg)
    risk_engine = RiskEngine(RiskLimits(require_stop_loss=False))
    price_state: dict[str, float] = {}
    execution_engine = PaperLocalExecutionEngine(
        price_provider=lambda s: price_state.get(s, 0.0), slippage_bps=0.0
    )
    ai_agent = AIShadowAgent(provider=SimulatedProvider(), enabled=False)
    orch = Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=market_data_provider, strategy_engine=strategy_engine,
        risk_engine=risk_engine, execution_engine=execution_engine, ai_agent=ai_agent,
        clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
    )

    with session_scope(session_factory) as session:
        repo.open_position(session, settings.symbol, side, 0.01, entry_price, stop_loss, take_profit)

    return orch


def test_buy_position_stop_loss_triggers_on_low_touch(session_factory):
    # BUY position, entry 100, stop 90: this candle's low crosses 90.
    candles = [make_candle(0, close=95)]
    candles[0] = candles[0].__class__(
        symbol="BTCUSDT", timeframe="1m", open_time=candles[0].open_time,
        open=95, high=96, low=85, close=95, volume=10, source="test", received_at=candles[0].received_at,
    )
    orch = build_orchestrator_with_open_position(session_factory, "BUY", 100.0, 90.0, 120.0, candles)
    result = orch.tick()
    assert result["status"] == "position_closed"
    assert result["realized_pnl"] < 0

    with session_scope(session_factory) as session:
        positions = repo.closed_positions(session)
        assert len(positions) == 1
        assert positions[0].status == "CLOSED"


def test_buy_position_take_profit_triggers_on_high_touch(session_factory):
    candles = [make_candle(0, close=110)]
    candles[0] = candles[0].__class__(
        symbol="BTCUSDT", timeframe="1m", open_time=candles[0].open_time,
        open=110, high=125, low=108, close=110, volume=10, source="test", received_at=candles[0].received_at,
    )
    orch = build_orchestrator_with_open_position(session_factory, "BUY", 100.0, 90.0, 120.0, candles)
    result = orch.tick()
    assert result["status"] == "position_closed"
    assert result["realized_pnl"] > 0


def test_sell_position_stop_loss_triggers_on_high_touch(session_factory):
    # SELL position: entry 100, stop 110 (price rising against us).
    candles = [make_candle(0, close=105)]
    candles[0] = candles[0].__class__(
        symbol="BTCUSDT", timeframe="1m", open_time=candles[0].open_time,
        open=105, high=115, low=104, close=105, volume=10, source="test", received_at=candles[0].received_at,
    )
    orch = build_orchestrator_with_open_position(session_factory, "SELL", 100.0, 110.0, 80.0, candles)
    result = orch.tick()
    assert result["status"] == "position_closed"
    assert result["realized_pnl"] < 0


def test_sell_position_take_profit_triggers_on_low_touch(session_factory):
    candles = [make_candle(0, close=85)]
    candles[0] = candles[0].__class__(
        symbol="BTCUSDT", timeframe="1m", open_time=candles[0].open_time,
        open=85, high=86, low=78, close=85, volume=10, source="test", received_at=candles[0].received_at,
    )
    orch = build_orchestrator_with_open_position(session_factory, "SELL", 100.0, 110.0, 80.0, candles)
    result = orch.tick()
    assert result["status"] == "position_closed"
    assert result["realized_pnl"] > 0


def test_both_stop_and_target_touched_same_candle_assumes_stop_first(session_factory):
    """Conservative rule (documented in docs/OPERACAO_DEMO.md): with no
    intrabar sequencing available, if a single candle's range crosses BOTH
    the stop and the target, the worse outcome (stop-loss) is assumed."""
    candle = make_candle(0, close=100)
    candle = candle.__class__(
        symbol="BTCUSDT", timeframe="1m", open_time=candle.open_time,
        open=100, high=115, low=85, close=100, volume=10, source="test", received_at=candle.received_at,
    )
    # BUY position entry=100, stop=90, target=110 -- this candle's range
    # [85, 115] crosses both.
    assert candle.low <= 90.0 and candle.high >= 110.0

    orch = build_orchestrator_with_open_position(session_factory, "BUY", 100.0, 90.0, 110.0, [candle])
    result = orch.tick()

    assert result["status"] == "position_closed"
    # Closed at the stop-loss price (90), a loss -- not at the take-profit
    # price (110), which would have been a gain.
    assert result["realized_pnl"] < 0

    with session_scope(session_factory) as session:
        signals = repo.recent_signals(session, limit=10)
        trigger_signal = next(s for s in signals if s.direction == "SELL")
        assert "stop-loss" in trigger_signal.justification.lower()
        assert "conservative" in trigger_signal.justification.lower()
