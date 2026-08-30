from __future__ import annotations

from pydantic import BaseModel


class SystemStateOut(BaseModel):
    mode: str
    trading_blocked: bool
    block_reason: str | None
    kill_switch_engaged: bool
    consecutive_losses: int
    cooldown_until: str | None
    api_failure_count: int


class MetricsOut(BaseModel):
    period_start: object
    period_end: object
    closed_trades_count: int
    gross_profit: object
    gross_loss: object
    net_profit: object
    commissions: object
    funding: object
    win_rate: object
    avg_win: object
    avg_loss: object
    payoff: object
    profit_factor: object
    expectancy: object
    max_win_streak: int
    max_loss_streak: int
    max_drawdown_money: object
    max_drawdown_pct: object
    return_on_capital_pct: object
    return_over_drawdown: object
    exposure_usd: object
