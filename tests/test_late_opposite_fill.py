"""Correção da Fase 2 v1.2 #5: `fill_service.apply_order_snapshot()` used
to pick the first open position for the symbol and, for an entry fill,
call `add_to_position()` WITHOUT checking that the position's side matched
the order's side. A late/opposite fill (arriving after the position
already flipped/closed) could get summed onto the wrong side, silently
corrupting the position. Now an entry fill is NEVER applied to a
position on the opposite side -- it is blocked (never fabricated) and
flagged for manual reconciliation.
"""
from __future__ import annotations

import pytest

from app.execution.base import FillEvent, OrderStatusSnapshot
from app.execution.order_state import OrderStatus
from app.execution import fill_service
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Order


def _make_order(session, symbol: str, side: str, idempotency_key: str, is_close: bool = False) -> Order:
    signal = repo.save_signal(session, symbol, side, "teste", 100.0, 1.0, {})
    risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
    order = repo.save_order(
        session, idempotency_key=idempotency_key, risk_evaluation_id=risk_eval.id,
        symbol=symbol, side=side, qty=0.01, stop_loss=90.0, take_profit=110.0, mode="BYBIT_DEMO",
        is_close=is_close,
    )
    repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
    order.exchange_order_id = f"EX-{idempotency_key}"
    return order


def test_late_entry_fill_after_an_opposing_position_exists_is_blocked(session_factory):
    with session_scope(session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 100.0, 90.0, 110.0)
        order = _make_order(session, "BTCUSDT", "SELL", "late-1")
        order_id = order.id
        state = repo.get_or_create_system_state(session)

        snapshot = OrderStatusSnapshot(
            exchange_order_id=order.exchange_order_id, status=OrderStatus.FILLED,
            fills=[FillEvent("EXEC-LATE-1", 0.01, 100.0, 0.001)],
        )
        result = fill_service.apply_order_snapshot(
            session, state, None, order, snapshot, is_close=False, max_api_failures=5,
        )
        assert state.state_ambiguous is True

    with session_scope(session_factory) as session:
        positions = repo.open_positions(session, "BTCUSDT")
        assert len(positions) == 1
        assert positions[0].side == "BUY"
        assert positions[0].qty == pytest.approx(0.01)  # unchanged -- never summed onto the wrong side

        events = repo.recent_security_events(session, limit=10)
        assert any(e.event_type == "LATE_OPPOSITE_FILL_BLOCKED" for e in events)

        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value  # the order itself still resolves (it WAS filled)
        assert order.filled_qty == pytest.approx(0.01)  # recorded in the ledger for audit/idempotency


def test_residual_close_fill_after_position_already_closed_is_still_a_safe_no_op(session_factory):
    """Regression guard: this case was already safe before the correction
    (position is None -> skip) -- proves it stays that way."""
    with session_scope(session_factory) as session:
        order = _make_order(session, "BTCUSDT", "SELL", "residual-close-1", is_close=True)
        order_id = order.id
        state = repo.get_or_create_system_state(session)

        snapshot = OrderStatusSnapshot(
            exchange_order_id=order.exchange_order_id, status=OrderStatus.FILLED,
            fills=[FillEvent("EXEC-RESIDUAL-1", 0.01, 100.0, 0.001)],
        )
        fill_service.apply_order_snapshot(
            session, state, None, order, snapshot, is_close=True, max_api_failures=5,
        )

    with session_scope(session_factory) as session:
        assert repo.open_positions(session, "BTCUSDT") == []  # no position fabricated
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value


def test_two_symbols_never_interfere_with_each_other(session_factory):
    """An opposite-side position in symbol B must never block or affect an
    entry fill for symbol A."""
    with session_scope(session_factory) as session:
        repo.open_position(session, "ETHUSDT", "SELL", 1.0, 2000.0, 2100.0, 1900.0)
        order = _make_order(session, "BTCUSDT", "BUY", "two-symbols-1")
        order_id = order.id
        state = repo.get_or_create_system_state(session)

        snapshot = OrderStatusSnapshot(
            exchange_order_id=order.exchange_order_id, status=OrderStatus.FILLED,
            fills=[FillEvent("EXEC-TWOSYM-1", 0.01, 100.0, 0.001)],
        )
        fill_service.apply_order_snapshot(
            session, state, None, order, snapshot, is_close=False, max_api_failures=5,
        )
        assert state.state_ambiguous is False

    with session_scope(session_factory) as session:
        btc_positions = repo.open_positions(session, "BTCUSDT")
        assert len(btc_positions) == 1
        assert btc_positions[0].side == "BUY"
        assert btc_positions[0].qty == pytest.approx(0.01)

        eth_positions = repo.open_positions(session, "ETHUSDT")
        assert len(eth_positions) == 1
        assert eth_positions[0].qty == pytest.approx(1.0)  # untouched

        events = repo.recent_security_events(session, limit=10)
        assert not any(e.event_type == "LATE_OPPOSITE_FILL_BLOCKED" for e in events)


def test_restart_between_position_flip_and_late_fill_arrival_still_blocks_correctly(session_factory):
    """Simulates a restart between the position changing side (e.g. closed
    and reopened opposite by a different, faster-resolving order) and a
    late fill for the ORIGINAL order finally arriving -- persisted state
    alone (no in-memory carry-over) must still catch the mismatch."""
    with session_scope(session_factory) as session:
        order = _make_order(session, "BTCUSDT", "BUY", "restart-late-1")
        order_id = order.id
        exchange_order_id = order.exchange_order_id

    # Position changes side entirely independently (e.g. a different,
    # faster order flipped it) -- committed and the session closed, exactly
    # as a real restart would leave only DB state behind.
    with session_scope(session_factory) as session:
        repo.open_position(session, "BTCUSDT", "SELL", 0.02, 105.0, 115.0, 95.0)

    # "Restart": a fresh session, no in-memory state from before, learns
    # about the late fill for the original BUY entry order.
    with session_scope(session_factory) as session:
        order = session.get(Order, order_id)
        state = repo.get_or_create_system_state(session)
        snapshot = OrderStatusSnapshot(
            exchange_order_id=exchange_order_id, status=OrderStatus.FILLED,
            fills=[FillEvent("EXEC-RESTART-LATE-1", 0.01, 100.0, 0.001)],
        )
        fill_service.apply_order_snapshot(
            session, state, None, order, snapshot, is_close=False, max_api_failures=5,
        )
        assert state.state_ambiguous is True

    with session_scope(session_factory) as session:
        positions = repo.open_positions(session, "BTCUSDT")
        assert len(positions) == 1
        assert positions[0].side == "SELL"
        assert positions[0].qty == pytest.approx(0.02)  # unchanged
