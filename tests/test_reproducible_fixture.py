"""Spec section 8: hand-compute expected metrics from the reproducible fixture
and assert the Metrics Engine matches exactly. This is the test an
independent auditor is meant to be able to re-derive by hand.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.metrics.engine import ClosedTrade, compute_metrics

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "reproducible_trades.json"


def load_fixture():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_reproducible_fixture_matches_hand_computed_metrics():
    data = load_fixture()
    starting_balance = data["starting_balance"]
    trades = [
        ClosedTrade(
            realized_pnl=t["realized_pnl"],
            fees_paid=t["fees_paid"],
            opened_at=datetime.fromisoformat(t["opened_at"]),
            closed_at=datetime.fromisoformat(t["closed_at"]),
        )
        for t in data["closed_trades"]
    ]

    result = compute_metrics(trades, starting_balance=starting_balance)

    # --- Hand computation, mirroring docs/METRICAS.md formulas ---
    pnls = [t["realized_pnl"] for t in data["closed_trades"]]
    fees = [t["fees_paid"] for t in data["closed_trades"]]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    expected_gross_profit = sum(wins)  # 120.0
    expected_gross_loss = sum(losses)  # -60.0
    expected_commissions = sum(fees)  # 6.0
    expected_net_profit = expected_gross_profit + expected_gross_loss - expected_commissions  # 54.0
    expected_win_rate = len(wins) / len(pnls)  # 0.5
    expected_avg_win = expected_gross_profit / len(wins)  # 40.0
    expected_avg_loss = expected_gross_loss / len(losses)  # -20.0
    expected_payoff = abs(expected_avg_win / expected_avg_loss)  # 2.0
    expected_profit_factor = expected_gross_profit / abs(expected_gross_loss)  # 2.0
    expected_expectancy = expected_win_rate * expected_avg_win + (1 - expected_win_rate) * expected_avg_loss  # 10.0

    equity = [starting_balance]
    running = starting_balance
    for pnl, fee in zip(pnls, fees):
        running += pnl - fee
        equity.append(running)
    peak = equity[0]
    max_dd_money = 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd_money = max(max_dd_money, peak - v)
    expected_max_dd_money = max_dd_money  # 63.0
    expected_max_dd_pct = max_dd_money / 1078.0 * 100.0

    expected_return_over_dd = expected_net_profit / expected_max_dd_money

    assert result.closed_trades_count == 6
    assert result.gross_profit == pytest.approx(expected_gross_profit)
    assert result.gross_loss == pytest.approx(expected_gross_loss)
    assert result.commissions == pytest.approx(expected_commissions)
    assert result.net_profit == pytest.approx(expected_net_profit)
    assert result.net_profit == pytest.approx(54.0)
    assert result.win_rate == pytest.approx(expected_win_rate)
    assert result.avg_win == pytest.approx(expected_avg_win)
    assert result.avg_loss == pytest.approx(expected_avg_loss)
    assert result.payoff == pytest.approx(expected_payoff)
    assert result.profit_factor == pytest.approx(expected_profit_factor)
    assert result.expectancy == pytest.approx(expected_expectancy)
    assert result.max_win_streak == 2
    assert result.max_loss_streak == 3
    assert result.max_drawdown_money == pytest.approx(expected_max_dd_money)
    assert result.max_drawdown_money == pytest.approx(63.0)
    assert result.max_drawdown_pct == pytest.approx(expected_max_dd_pct)
    assert result.return_over_drawdown == pytest.approx(expected_return_over_dd)


def test_reproducible_fixture_open_position_excluded_from_realized_pnl():
    data = load_fixture()
    open_pos = data["open_position"]
    trades = [
        ClosedTrade(
            realized_pnl=t["realized_pnl"], fees_paid=t["fees_paid"],
            opened_at=datetime.fromisoformat(t["opened_at"]),
            closed_at=datetime.fromisoformat(t["closed_at"]),
        )
        for t in data["closed_trades"]
    ]
    open_exposure = open_pos["qty"] * open_pos["avg_entry_price"]
    result = compute_metrics(trades, starting_balance=data["starting_balance"], open_exposure_usd=open_exposure)
    assert result.net_profit == pytest.approx(54.0)
    assert result.exposure_usd == pytest.approx(21.0)
