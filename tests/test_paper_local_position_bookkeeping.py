"""Correction v1.1 #9: PaperLocalExecutionEngine._apply_to_position must
handle all four cases correctly instead of naively summing every fill onto
whatever position dict already exists.
"""
from __future__ import annotations

import pytest

from app.execution.idempotency import make_idempotency_key
from app.execution.paper_local import PaperLocalExecutionEngine
from tests.factories import approved_open_order


def order(side, qty, signal_id=1):
    return approved_open_order(
        symbol="BTCUSDT", side=side, qty=qty, price=40000.0,
        stop_loss=None, take_profit=None, signal_id=signal_id,
    )


def test_case1_no_existing_position_opens_new_one():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, slippage_bps=0)
    o = order("BUY", 0.01)
    engine.submit(o, make_idempotency_key(o, "b1"), reference_price=40000.0)
    pos = engine.get_position("BTCUSDT")
    assert pos["side"] == "BUY"
    assert pos["qty"] == pytest.approx(0.01)
    assert pos["avg_entry_price"] == pytest.approx(40000.0)


def test_case2_same_side_increases_qty_and_recomputes_avg_price():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, slippage_bps=0)
    o1 = order("BUY", 0.01, signal_id=1)
    engine.submit(o1, make_idempotency_key(o1, "b1"), reference_price=40000.0)
    o2 = order("BUY", 0.01, signal_id=2)
    engine.submit(o2, make_idempotency_key(o2, "b2"), reference_price=42000.0)

    pos = engine.get_position("BTCUSDT")
    assert pos["qty"] == pytest.approx(0.02)
    # weighted avg: (40000*0.01 + 42000*0.01) / 0.02 = 41000
    assert pos["avg_entry_price"] == pytest.approx(41000.0)


def test_case3_opposite_side_smaller_qty_reduces_position():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, slippage_bps=0)
    o1 = order("BUY", 0.02, signal_id=1)
    engine.submit(o1, make_idempotency_key(o1, "b1"), reference_price=40000.0)
    o2 = order("SELL", 0.005, signal_id=2)
    engine.submit(o2, make_idempotency_key(o2, "b2"), reference_price=41000.0)

    pos = engine.get_position("BTCUSDT")
    assert pos["side"] == "BUY"
    assert pos["qty"] == pytest.approx(0.015)
    assert pos["avg_entry_price"] == pytest.approx(40000.0)  # unchanged by a reduce


def test_case3b_opposite_side_exact_qty_closes_position():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, slippage_bps=0)
    o1 = order("BUY", 0.02, signal_id=1)
    engine.submit(o1, make_idempotency_key(o1, "b1"), reference_price=40000.0)
    o2 = order("SELL", 0.02, signal_id=2)
    engine.submit(o2, make_idempotency_key(o2, "b2"), reference_price=41000.0)

    assert engine.get_position("BTCUSDT") is None


def test_case4_opposite_side_larger_qty_closes_and_flips():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, slippage_bps=0)
    o1 = order("BUY", 0.01, signal_id=1)
    engine.submit(o1, make_idempotency_key(o1, "b1"), reference_price=40000.0)
    o2 = order("SELL", 0.03, signal_id=2)
    engine.submit(o2, make_idempotency_key(o2, "b2"), reference_price=41000.0)

    pos = engine.get_position("BTCUSDT")
    assert pos["side"] == "SELL"
    assert pos["qty"] == pytest.approx(0.02)  # excess = 0.03 - 0.01
    assert pos["avg_entry_price"] == pytest.approx(41000.0)


def test_fills_never_simply_summed_across_opposite_sides():
    """Regression guard for the original bug: an opposite-side fill must
    never just be added on top of the existing quantity."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, slippage_bps=0)
    o1 = order("BUY", 0.02, signal_id=1)
    engine.submit(o1, make_idempotency_key(o1, "b1"), reference_price=40000.0)
    o2 = order("SELL", 0.01, signal_id=2)
    engine.submit(o2, make_idempotency_key(o2, "b2"), reference_price=41000.0)

    pos = engine.get_position("BTCUSDT")
    assert pos["qty"] != pytest.approx(0.03)  # would be the bug: 0.02 + 0.01
    assert pos["qty"] == pytest.approx(0.01)
