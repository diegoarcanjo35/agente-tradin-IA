"""Fase 2, item 7.2 / correção v1.1 #2: repo-level order lifecycle
primitives -- transition_order_status (the only sanctioned way to change
Order.status, validated + audited) and the persistent, idempotent fill
ledger (app.execution.fill_ledger.record_new_fills) -- fills are applied by
DELTA from individually-recorded Execution rows, never by overwriting
cumulative totals."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.execution.base import FillEvent
from app.execution.fill_ledger import record_new_fills
from app.execution.order_state import IllegalOrderTransitionError, OrderStatus
from app.persistence import repo
from app.persistence.models import Execution, OrderEvent


def _make_order(session):
    signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
    risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
    return repo.save_order(
        session, idempotency_key="k1", risk_evaluation_id=risk_eval.id, symbol="BTCUSDT",
        side="BUY", qty=0.01, stop_loss=90.0, take_profit=110.0, mode="PAPER_LOCAL",
    )


def test_new_order_starts_at_pending_submit(db_session):
    order = _make_order(db_session)
    assert order.status == OrderStatus.PENDING_SUBMIT.value
    assert order.filled_qty == 0.0
    assert order.avg_fill_price == 0.0
    assert order.fees_total == 0.0


def test_valid_transition_updates_status_and_writes_audit_event(db_session):
    order = _make_order(db_session)
    repo.transition_order_status(db_session, order, OrderStatus.SUBMITTED, detail="aceita pela corretora")
    assert order.status == OrderStatus.SUBMITTED.value

    events = db_session.execute(select(OrderEvent).where(OrderEvent.order_id == order.id)).scalars().all()
    assert len(events) == 1
    assert events[0].from_status == OrderStatus.PENDING_SUBMIT.value
    assert events[0].to_status == OrderStatus.SUBMITTED.value
    assert events[0].detail == "aceita pela corretora"


def test_illegal_transition_raises_and_never_mutates_status_or_writes_event(db_session):
    order = _make_order(db_session)  # PENDING_SUBMIT
    try:
        repo.transition_order_status(db_session, order, OrderStatus.CANCELLED)
    except IllegalOrderTransitionError:
        pass
    else:
        raise AssertionError("expected IllegalOrderTransitionError")

    assert order.status == OrderStatus.PENDING_SUBMIT.value  # unchanged
    events = db_session.execute(select(OrderEvent).where(OrderEvent.order_id == order.id)).scalars().all()
    assert events == []  # no audit row for a transition that never happened


def test_record_new_fills_recomputes_cumulative_totals_from_the_ledger_by_delta(db_session):
    order = _make_order(db_session)
    order.qty = 0.01
    repo.transition_order_status(db_session, order, OrderStatus.SUBMITTED)

    new_rows = record_new_fills(db_session, order, [
        FillEvent(exchange_fill_id="EXEC-1", fill_qty=0.004, fill_price=100.0, fee=0.001),
    ])
    assert len(new_rows) == 1
    assert order.filled_qty == pytest.approx(0.004)
    assert order.avg_fill_price == pytest.approx(100.0)
    assert order.fees_total == pytest.approx(0.001)

    # A second, LATER individual fill (a genuinely new exchange_fill_id) --
    # totals must be recomputed from BOTH fills, weighted by quantity, never
    # just the newest one's numbers.
    new_rows = record_new_fills(db_session, order, [
        FillEvent(exchange_fill_id="EXEC-1", fill_qty=0.004, fill_price=100.0, fee=0.001),  # already recorded
        FillEvent(exchange_fill_id="EXEC-2", fill_qty=0.003, fill_price=100.4, fee=0.0007),
    ])
    assert len(new_rows) == 1  # EXEC-1 was already recorded -- only EXEC-2 is new
    assert new_rows[0].exchange_fill_id == "EXEC-2"
    assert order.filled_qty == pytest.approx(0.007)
    expected_avg = (0.004 * 100.0 + 0.003 * 100.4) / 0.007
    assert order.avg_fill_price == pytest.approx(expected_avg)
    assert order.fees_total == pytest.approx(0.0017)

    # Completion.
    record_new_fills(db_session, order, [
        FillEvent(exchange_fill_id="EXEC-3", fill_qty=0.003, fill_price=100.15, fee=0.0004),
    ])
    repo.transition_order_status(db_session, order, OrderStatus.FILLED)
    assert order.filled_qty == pytest.approx(0.01)

    rows = db_session.execute(
        select(Execution).where(Execution.order_id == order.id).order_by(Execution.id)
    ).scalars().all()
    assert [r.exchange_fill_id for r in rows] == ["EXEC-1", "EXEC-2", "EXEC-3"]  # each recorded exactly once


def test_record_new_fills_repeated_with_the_same_exchange_fill_id_is_a_safe_no_op(db_session):
    """Simulates re-polling the same order and getting the exact same fill
    history back every time -- must never double-count."""
    order = _make_order(db_session)
    order.qty = 0.01
    repo.transition_order_status(db_session, order, OrderStatus.SUBMITTED)

    fills = [FillEvent(exchange_fill_id="EXEC-1", fill_qty=0.01, fill_price=100.0, fee=0.001)]
    record_new_fills(db_session, order, fills)
    repeated = record_new_fills(db_session, order, fills)
    assert repeated == []  # nothing new
    assert order.filled_qty == pytest.approx(0.01)  # not 0.02

    count = db_session.execute(
        select(Execution).where(Execution.order_id == order.id)
    ).scalars().all()
    assert len(count) == 1
