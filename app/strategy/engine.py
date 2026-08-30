"""Deterministic, auditable strategy: moving-average crossover, gated by a
trend filter and an ATR-based volatility filter. No ML, no black box -- every
signal carries the exact numbers that produced it.

This strategy makes no promise of profitability; see docs/OPERACAO_DEMO.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.clock import utcnow
from app.market_data.base import CandleTick
from app.strategy.schemas import Signal


@dataclass
class StrategyConfig:
    fast_period: int = 9
    slow_period: int = 21
    atr_period: int = 14
    min_atr_pct_of_price: float = 0.0005  # below this, market judged too quiet to trade
    max_atr_pct_of_price: float = 0.05  # above this, market judged too volatile to trade
    stop_loss_atr_multiple: float = 2.0
    take_profit_atr_multiple: float = 3.0


class StrategyEngine:
    def __init__(self, symbol: str, config: StrategyConfig | None = None):
        self.symbol = symbol
        self.config = config or StrategyConfig()
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._prev_fast_above_slow: bool | None = None

    def _sma(self, values: list[float], period: int) -> float | None:
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    def _atr(self) -> float | None:
        period = self.config.atr_period
        if len(self._closes) < period + 1:
            return None
        true_ranges = []
        for i in range(-period, 0):
            high = self._highs[i]
            low = self._lows[i]
            prev_close = self._closes[i - 1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        return sum(true_ranges) / period

    def on_candle(self, candle: CandleTick) -> Signal:
        self._closes.append(candle.close)
        self._highs.append(candle.high)
        self._lows.append(candle.low)

        cfg = self.config
        fast = self._sma(self._closes, cfg.fast_period)
        slow = self._sma(self._closes, cfg.slow_period)
        atr = self._atr()

        params = {
            "fast_period": cfg.fast_period,
            "slow_period": cfg.slow_period,
            "atr_period": cfg.atr_period,
            "fast_sma": fast,
            "slow_sma": slow,
            "atr": atr,
        }

        if fast is None or slow is None or atr is None:
            self._prev_fast_above_slow = None if fast is None or slow is None else fast > slow
            return Signal(
                symbol=self.symbol, direction="HOLD",
                justification="Insufficient history to compute indicators yet.",
                created_at=utcnow(), observed_price=candle.close, atr=atr or 0.0,
                stop_loss=None, take_profit=None, params=params,
            )

        atr_pct = atr / candle.close if candle.close else 0.0
        if atr_pct < cfg.min_atr_pct_of_price:
            self._prev_fast_above_slow = fast > slow
            return Signal(
                symbol=self.symbol, direction="HOLD",
                justification=f"ATR% {atr_pct:.5f} below min volatility filter {cfg.min_atr_pct_of_price}.",
                created_at=utcnow(), observed_price=candle.close, atr=atr,
                stop_loss=None, take_profit=None, params=params,
            )
        if atr_pct > cfg.max_atr_pct_of_price:
            self._prev_fast_above_slow = fast > slow
            return Signal(
                symbol=self.symbol, direction="HOLD",
                justification=f"ATR% {atr_pct:.5f} above max volatility filter {cfg.max_atr_pct_of_price}.",
                created_at=utcnow(), observed_price=candle.close, atr=atr,
                stop_loss=None, take_profit=None, params=params,
            )

        fast_above_slow = fast > slow
        direction = "HOLD"
        justification = f"No crossover: fast_sma={fast:.2f} slow_sma={slow:.2f}."
        stop_loss = None
        take_profit = None

        if self._prev_fast_above_slow is not None and fast_above_slow != self._prev_fast_above_slow:
            if fast_above_slow:
                direction = "BUY"
                justification = (
                    f"Bullish crossover: fast_sma({cfg.fast_period})={fast:.2f} crossed above "
                    f"slow_sma({cfg.slow_period})={slow:.2f}; trend filter and ATR volatility "
                    f"filter (ATR%={atr_pct:.5f}) passed."
                )
                stop_loss = candle.close - cfg.stop_loss_atr_multiple * atr
                take_profit = candle.close + cfg.take_profit_atr_multiple * atr
            else:
                direction = "SELL"
                justification = (
                    f"Bearish crossover: fast_sma({cfg.fast_period})={fast:.2f} crossed below "
                    f"slow_sma({cfg.slow_period})={slow:.2f}; trend filter and ATR volatility "
                    f"filter (ATR%={atr_pct:.5f}) passed."
                )
                stop_loss = candle.close + cfg.stop_loss_atr_multiple * atr
                take_profit = candle.close - cfg.take_profit_atr_multiple * atr

        self._prev_fast_above_slow = fast_above_slow

        return Signal(
            symbol=self.symbol, direction=direction, justification=justification,
            created_at=utcnow(), observed_price=candle.close, atr=atr,
            stop_loss=stop_loss, take_profit=take_profit, params=params,
        )
