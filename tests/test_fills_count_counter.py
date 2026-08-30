"""Correção da Fase 2 v1.2 #6: `fills_count` used to be incremented ONCE
per `apply_order_snapshot()` call that produced any new fill row(s),
regardless of how many. A single snapshot reporting 3 new fills only
counted as 1. `fills_count` now counts individual fills.
"""
from __future__ import annotations

from app.execution import fill_service
from app.execution.base import FillEvent, OrderStatusSnapshot
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import OperationalSession, Order


def _make_order_and_session(session) -> tuple[Order, OperationalSession]:
    signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
    risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
    order = repo.save_order(
        session, idempotency_key="fills-count-1", risk_evaluation_id=risk_eval.id,
        symbol="BTCUSDT", side="BUY", qty=1.0, stop_loss=90.0, take_profit=110.0, mode="BYBIT_DEMO",
    )
    repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
    order.exchange_order_id = "EX-FILLSCOUNT-1"

    op_session = OperationalSession(
        session_uid="uid-fills-count", mode="BYBIT_DEMO", symbol="BTCUSDT", timeframe="1",
        strategy_version="v1", risk_config_json="{}", config_snapshot_json="{}",
    )
    session.add(op_session)
    session.flush()
    return order, op_session


def test_a_snapshot_with_three_new_fills_increments_fills_count_by_three(session_factory):
    with session_scope(session_factory) as session:
        order, op_session = _make_order_and_session(session)
        state = repo.get_or_create_system_state(session)

        snapshot = OrderStatusSnapshot(
            exchange_order_id=order.exchange_order_id, status=OrderStatus.PARTIALLY_FILLED,
            fills=[
                FillEvent("EXEC-A", 0.3, 100.0, 0.001),
                FillEvent("EXEC-B", 0.3, 100.1, 0.001),
                FillEvent("EXEC-C", 0.3, 100.2, 0.001),
            ],
        )
        fill_service.apply_order_snapshot(
            session, state, op_session, order, snapshot, is_close=False, max_api_failures=5,
        )
        assert op_session.fills_count == 3


def test_repeating_the_same_snapshot_is_idempotent_and_does_not_double_count(session_factory):
    with session_scope(session_factory) as session:
        order, op_session = _make_order_and_session(session)
        state = repo.get_or_create_system_state(session)
        order_id, op_session_id = order.id, op_session.id

        snapshot = OrderStatusSnapshot(
            exchange_order_id=order.exchange_order_id, status=OrderStatus.PARTIALLY_FILLED,
            fills=[
                FillEvent("EXEC-D", 0.3, 100.0, 0.001),
                FillEvent("EXEC-E", 0.3, 100.1, 0.001),
                FillEvent("EXEC-F", 0.3, 100.2, 0.001),
            ],
        )
        fill_service.apply_order_snapshot(
            session, state, op_session, order, snapshot, is_close=False, max_api_failures=5,
        )

    # poll_order() always reports the FULL history -- repeating the exact
    # same snapshot (e.g. a redundant re-poll) must not re-count anything.
    with session_scope(session_factory) as session:
        order = session.get(Order, order_id)
        op_session = session.get(OperationalSession, op_session_id)
        state = repo.get_or_create_system_state(session)

        snapshot = OrderStatusSnapshot(
            exchange_order_id=order.exchange_order_id, status=OrderStatus.PARTIALLY_FILLED,
            fills=[
                FillEvent("EXEC-D", 0.3, 100.0, 0.001),
                FillEvent("EXEC-E", 0.3, 100.1, 0.001),
                FillEvent("EXEC-F", 0.3, 100.2, 0.001),
            ],
        )
        fill_service.apply_order_snapshot(
            session, state, op_session, order, snapshot, is_close=False, max_api_failures=5,
        )
        assert op_session.fills_count == 3  # still 3, not 6

    # One more, genuinely NEW fill afterward -- must add exactly 1.
    with session_scope(session_factory) as session:
        order = session.get(Order, order_id)
        op_session = session.get(OperationalSession, op_session_id)
        state = repo.get_or_create_system_state(session)

        snapshot = OrderStatusSnapshot(
            exchange_order_id=order.exchange_order_id, status=OrderStatus.FILLED,
            fills=[
                FillEvent("EXEC-D", 0.3, 100.0, 0.001),
                FillEvent("EXEC-E", 0.3, 100.1, 0.001),
                FillEvent("EXEC-F", 0.3, 100.2, 0.001),
                FillEvent("EXEC-G", 0.1, 100.3, 0.0005),
            ],
        )
        fill_service.apply_order_snapshot(
            session, state, op_session, order, snapshot, is_close=False, max_api_failures=5,
        )
        assert op_session.fills_count == 4
