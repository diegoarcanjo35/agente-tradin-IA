"""Correction v1.1 #5: commissions must accumulate every execution across a
position's life (opening fee + partial-fill fees + closing fee), never just
the last one. Exercises the actual persistence helpers that were the source
of the bug (app/persistence/repo.py::open_position/add_to_position/
close_position), then feeds the resulting position through the real Metrics
Engine and proves net = gross - ALL fees.
"""
from __future__ import annotations

import pytest

from app.metrics.engine import ClosedTrade, compute_metrics
from app.persistence import repo


def test_fees_accumulate_across_open_partial_fills_and_close(db_session):
    session = db_session

    # Opening fill: qty 0.01 @ 100, fee 0.05
    position = repo.open_position(
        session, symbol="BTCUSDT", side="BUY", qty=0.01, avg_entry_price=100.0,
        stop_loss=90.0, take_profit=130.0, opening_fee=0.05,
    )
    assert position.fees_paid == pytest.approx(0.05)

    # Two additional partial fills adding to the position (same side), each
    # with their own fee.
    repo.add_to_position(session, position, additional_qty=0.01, fill_price=110.0, fee=0.06)
    assert position.fees_paid == pytest.approx(0.11)
    assert position.qty == pytest.approx(0.02)
    assert position.avg_entry_price == pytest.approx((100.0 * 0.01 + 110.0 * 0.01) / 0.02)

    repo.add_to_position(session, position, additional_qty=0.01, fill_price=120.0, fee=0.07)
    assert position.fees_paid == pytest.approx(0.18)
    assert position.qty == pytest.approx(0.03)

    # Close the whole position at 150: gross P&L known exactly from the
    # weighted average entry price.
    avg_entry = position.avg_entry_price
    close_price = 150.0
    gross_pnl = (close_price - avg_entry) * position.qty
    closing_fee = 0.09

    repo.close_position(session, position, realized_pnl_delta=gross_pnl, closing_fee=closing_fee)

    total_fees_expected = 0.05 + 0.06 + 0.07 + 0.09
    assert position.fees_paid == pytest.approx(total_fees_expected)
    assert position.realized_pnl == pytest.approx(gross_pnl)

    # Feed through the real Metrics Engine and verify net = gross - ALL fees,
    # not just the closing fee.
    trade = ClosedTrade(
        realized_pnl=position.realized_pnl, fees_paid=position.fees_paid,
        opened_at=position.opened_at, closed_at=position.closed_at,
    )
    result = compute_metrics([trade], starting_balance=1000.0)

    assert result.commissions == pytest.approx(total_fees_expected)
    assert result.net_profit == pytest.approx(gross_pnl - total_fees_expected)
    # Regression guard: net must NOT equal gross minus only the last fee.
    assert result.net_profit != pytest.approx(gross_pnl - closing_fee)


def test_reduce_position_also_accumulates_fees_without_closing(db_session):
    session = db_session
    position = repo.open_position(
        session, symbol="ETHUSDT", side="BUY", qty=0.1, avg_entry_price=2000.0,
        stop_loss=1900.0, take_profit=2200.0, opening_fee=1.0,
    )
    repo.reduce_position(session, position, reduce_qty=0.04, realized_pnl_delta=8.0, fee=0.5)

    assert position.status == "OPEN"
    assert position.qty == pytest.approx(0.06)
    assert position.realized_pnl == pytest.approx(8.0)
    assert position.fees_paid == pytest.approx(1.5)
