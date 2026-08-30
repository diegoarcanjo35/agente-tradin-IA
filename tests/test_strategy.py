from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market_data.base import CandleTick
from app.strategy.engine import StrategyConfig, StrategyEngine

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def make_candle(i: int, close: float) -> CandleTick:
    t = T0 + timedelta(minutes=i)
    return CandleTick(
        symbol="BTCUSDT", timeframe="1m", open_time=t, open=close, high=close + 5,
        low=close - 5, close=close, volume=100.0, source="replay", received_at=t,
    )


def test_hold_while_insufficient_history():
    engine = StrategyEngine("BTCUSDT", StrategyConfig(fast_period=3, slow_period=5, atr_period=3))
    signal = engine.on_candle(make_candle(0, 100.0))
    assert signal.direction == "HOLD"


def test_signal_always_carries_traceable_params():
    engine = StrategyEngine("BTCUSDT", StrategyConfig(fast_period=3, slow_period=5, atr_period=3))
    signal = engine.on_candle(make_candle(0, 100.0))
    assert "fast_period" in signal.params
    assert "slow_period" in signal.params
    assert signal.symbol == "BTCUSDT"


def test_buy_signal_on_bullish_crossover():
    cfg = StrategyConfig(
        fast_period=2, slow_period=4, atr_period=2,
        min_atr_pct_of_price=0.0, max_atr_pct_of_price=1.0,
    )
    engine = StrategyEngine("BTCUSDT", cfg)
    # Downtrend first so fast < slow, then a sharp rally to force a crossover.
    prices = [100, 99, 98, 97, 96, 120, 140]
    directions = []
    for i, p in enumerate(prices):
        signal = engine.on_candle(make_candle(i, p))
        directions.append(signal.direction)
        if signal.direction == "BUY":
            assert signal.stop_loss < signal.observed_price
            assert signal.take_profit > signal.observed_price
    assert "BUY" in directions


def test_hold_when_volatility_filter_blocks():
    cfg = StrategyConfig(
        fast_period=2, slow_period=3, atr_period=2,
        min_atr_pct_of_price=10.0,  # impossibly high -> always blocked
        max_atr_pct_of_price=100.0,
    )
    engine = StrategyEngine("BTCUSDT", cfg)
    directions = [engine.on_candle(make_candle(i, 100 + i)).direction for i in range(6)]
    assert all(d == "HOLD" for d in directions)
