"""Pure functions computing metrics exclusively from persisted rows. Nothing
here talks to the database or the exchange -- it takes plain data in and
returns a MetricsResult, which is what makes it independently auditable and
unit-testable against a hand-computed fixture (see
tests/test_reproducible_fixture.py).

Every metric that cannot be computed from the given data reports the string
sentinel UNAVAILABLE rather than a fabricated 0, per the "não inventar zero"
requirement.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Union

UNAVAILABLE = "indisponível"

Metric = Union[float, int, str]


@dataclass(frozen=True)
class ClosedTrade:
    """Minimal view of a closed Position the metrics engine needs."""

    realized_pnl: float  # gross, excluding fees
    fees_paid: float
    opened_at: datetime
    closed_at: datetime


@dataclass(frozen=True)
class MetricsResult:
    period_start: Metric
    period_end: Metric
    closed_trades_count: int
    gross_profit: Metric
    gross_loss: Metric
    net_profit: Metric
    commissions: Metric
    funding: Metric
    win_rate: Metric
    avg_win: Metric
    avg_loss: Metric
    payoff: Metric
    profit_factor: Metric
    expectancy: Metric
    max_win_streak: int
    max_loss_streak: int
    max_drawdown_money: Metric
    max_drawdown_pct: Metric
    return_on_capital_pct: Metric
    return_over_drawdown: Metric
    exposure_usd: Metric


@dataclass(frozen=True)
class OrderFillView:
    """Minimal view of a filled Order the cost/slippage metrics need
    (Fase 2, item 7.6)."""

    side: str  # BUY | SELL
    reference_price: float | None
    avg_fill_price: float
    fees_total: float


@dataclass(frozen=True)
class CostMetricsResult:
    fees_total: Metric
    slippage_avg_usd: Metric
    slippage_total_usd: Metric
    priced_orders_count: int
    unpriced_orders_count: int


def compute_cost_metrics(orders: list[OrderFillView]) -> CostMetricsResult:
    """Fase 2, item 7.6: fees accumulated and realized slippage versus the
    reference price each order was decided against. Never invents a zero --
    `fees_total` still sums whatever is known even with zero orders (0.0 is
    a genuine, correct total for an empty set, not a fabricated one);
    slippage is UNAVAILABLE only when NO order in the set has a known
    reference_price (e.g. orders persisted before this field existed)."""
    fees_total = sum(o.fees_total for o in orders)
    priced = [o for o in orders if o.reference_price is not None]
    unpriced_count = len(orders) - len(priced)

    if not priced:
        return CostMetricsResult(
            fees_total=fees_total, slippage_avg_usd=UNAVAILABLE, slippage_total_usd=UNAVAILABLE,
            priced_orders_count=0, unpriced_orders_count=unpriced_count,
        )

    slippages = []
    for o in priced:
        # BUY: paying MORE than the reference price is adverse (positive
        # here = worse for the trader). SELL: receiving LESS is adverse.
        if o.side == "BUY":
            slippages.append(o.avg_fill_price - o.reference_price)
        else:
            slippages.append(o.reference_price - o.avg_fill_price)

    return CostMetricsResult(
        fees_total=fees_total,
        slippage_avg_usd=sum(slippages) / len(slippages),
        slippage_total_usd=sum(slippages),
        priced_orders_count=len(priced),
        unpriced_orders_count=unpriced_count,
    )


def _streaks(pnls: list[float]) -> tuple[int, int]:
    max_win = cur_win = 0
    max_loss = cur_loss = 0
    for pnl in pnls:
        if pnl > 0:
            cur_win += 1
            cur_loss = 0
        elif pnl < 0:
            cur_loss += 1
            cur_win = 0
        else:
            cur_win = 0
            cur_loss = 0
        max_win = max(max_win, cur_win)
        max_loss = max(max_loss, cur_loss)
    return max_win, max_loss


def _max_drawdown(equity_curve: list[float]) -> tuple[float, float]:
    """Returns (max_drawdown_money, max_drawdown_pct) over a non-empty curve."""
    peak = equity_curve[0]
    max_dd_money = 0.0
    max_dd_pct = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        dd_money = peak - value
        dd_pct = (dd_money / peak * 100.0) if peak > 0 else 0.0
        max_dd_money = max(max_dd_money, dd_money)
        max_dd_pct = max(max_dd_pct, dd_pct)
    return max_dd_money, max_dd_pct


def compute_metrics(
    closed_trades: list[ClosedTrade],
    starting_balance: float,
    open_exposure_usd: float | None = None,
    funding_total: float | None = None,
) -> MetricsResult:
    """Correção v1.1 #6: `funding_total`, when given (a real collected SUM
    from app.persistence.repo.funding_total -- only ever available for
    BYBIT_DEMO), is reported as `funding` and contributes to `net_profit`.
    `None` (no funding_provider at all -- REPLAY/PAPER_LOCAL/PAPER_LIVE)
    keeps the exact pre-existing UNAVAILABLE behavior; it is never
    conflated with a genuine 0.0 (no funding settled yet, but a provider
    exists and reached the exchange)."""
    funding: Metric = funding_total if funding_total is not None else UNAVAILABLE
    funding_component = funding_total if funding_total is not None else 0.0

    n = len(closed_trades)
    if n == 0:
        return MetricsResult(
            period_start=UNAVAILABLE, period_end=UNAVAILABLE, closed_trades_count=0,
            gross_profit=UNAVAILABLE, gross_loss=UNAVAILABLE, net_profit=UNAVAILABLE,
            commissions=UNAVAILABLE, funding=funding, win_rate=UNAVAILABLE,
            avg_win=UNAVAILABLE, avg_loss=UNAVAILABLE, payoff=UNAVAILABLE,
            profit_factor=UNAVAILABLE, expectancy=UNAVAILABLE, max_win_streak=0,
            max_loss_streak=0, max_drawdown_money=UNAVAILABLE, max_drawdown_pct=UNAVAILABLE,
            return_on_capital_pct=UNAVAILABLE, return_over_drawdown=UNAVAILABLE,
            exposure_usd=open_exposure_usd if open_exposure_usd is not None else UNAVAILABLE,
        )

    ordered = sorted(closed_trades, key=lambda t: t.closed_at)
    pnls = [t.realized_pnl for t in ordered]
    fees = [t.fees_paid for t in ordered]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_profit = sum(wins)
    gross_loss = sum(losses)  # negative or zero
    commissions = sum(fees)
    net_profit = gross_profit + gross_loss - commissions + funding_component

    win_rate = len(wins) / n
    avg_win = (gross_profit / len(wins)) if wins else UNAVAILABLE
    avg_loss = (gross_loss / len(losses)) if losses else UNAVAILABLE

    payoff: Metric
    if isinstance(avg_win, float) and isinstance(avg_loss, float) and avg_loss != 0:
        payoff = abs(avg_win / avg_loss)
    else:
        payoff = UNAVAILABLE

    profit_factor: Metric = (gross_profit / abs(gross_loss)) if gross_loss < 0 else UNAVAILABLE

    expectancy: Metric
    if isinstance(avg_win, float) and isinstance(avg_loss, float):
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    elif wins and not losses:
        expectancy = win_rate * avg_win
    else:
        expectancy = UNAVAILABLE

    max_win_streak, max_loss_streak = _streaks(pnls)

    equity_curve = [starting_balance]
    running = starting_balance
    for pnl, fee in zip(pnls, fees):
        running += pnl - fee
        equity_curve.append(running)

    max_dd_money, max_dd_pct = _max_drawdown(equity_curve)

    return_on_capital_pct: Metric = (
        (net_profit / starting_balance * 100.0) if starting_balance > 0 else UNAVAILABLE
    )
    return_over_drawdown: Metric = (net_profit / max_dd_money) if max_dd_money > 0 else UNAVAILABLE

    return MetricsResult(
        period_start=ordered[0].opened_at.isoformat(),
        period_end=ordered[-1].closed_at.isoformat(),
        closed_trades_count=n,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_profit=net_profit,
        commissions=commissions,
        funding=funding,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff=payoff,
        profit_factor=profit_factor,
        expectancy=expectancy,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        max_drawdown_money=max_dd_money,
        max_drawdown_pct=max_dd_pct,
        return_on_capital_pct=return_on_capital_pct,
        return_over_drawdown=return_over_drawdown,
        exposure_usd=open_exposure_usd if open_exposure_usd is not None else UNAVAILABLE,
    )
