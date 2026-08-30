"""Fase 2, item 7.2: repo-level order lifecycle primitives --
transition_order_status (the only sanctioned way to change Order.status,
validated + audited) and record_fill (cumulative, set-semantics fill
bookkeeping)."""
from __future__ import annotations

from sqlalchemy import select

from app.execution.order_state import IllegalOrderTransitionError, OrderStatus
from app.persistence import repo
from app.persistence.models import OrderEvent


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


def test_record_fill_sets_cumulative_totals_not_increments(db_session):
    order = _make_order(db_session)
    repo.transition_order_status(db_session, order, OrderStatus.SUBMITTED)

    repo.record_fill(
        db_session, order, OrderStatus.PARTIALLY_FILLED,
        cumulative_filled_qty=0.004, avg_fill_price=100.0, fees_total=0.001,
    )
    assert order.filled_qty == 0.004
    assert order.avg_fill_price == 100.0
    assert order.fees_total == 0.001

    # A second, LATER partial fill reports the exchange's new cumulative
    # totals (never the delta) -- record_fill must SET, not add.
    repo.record_fill(
        db_session, order, OrderStatus.PARTIALLY_FILLED,
        cumulative_filled_qty=0.007, avg_fill_price=100.2, fees_total=0.0025,
    )
    assert order.filled_qty == 0.007  # not 0.004 + 0.007
    assert order.avg_fill_price == 100.2
    assert order.fees_total == 0.0025

    # Completion.
    repo.record_fill(
        db_session, order, OrderStatus.FILLED,
        cumulative_filled_qty=0.01, avg_fill_price=100.15, fees_total=0.004,
    )
    assert order.status == OrderStatus.FILLED.value
    assert order.filled_qty == 0.01

    events = db_session.execute(
        select(OrderEvent).where(OrderEvent.order_id == order.id).order_by(OrderEvent.id)
    ).scalars().all()
    assert [e.to_status for e in events] == [
        OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.PARTIALLY_FILLED.value, OrderStatus.FILLED.value,
    ]


def test_record_fill_on_already_terminal_order_is_rejected():
    """A poller must check is_terminal() before calling record_fill again --
    record_fill itself correctly refuses to "re-fill" a terminal order."""
    import app.persistence.db as db_module

    engine = db_module.make_engine("sqlite:///:memory:")
    db_module.init_db(engine)
    session_factory = db_module.make_session_factory(engine)
    with db_module.session_scope(session_factory) as session:
        order = _make_order(session)
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        repo.record_fill(session, order, OrderStatus.FILLED, 0.01, 100.0, 0.001)

        try:
            repo.record_fill(session, order, OrderStatus.FILLED, 0.01, 100.0, 0.001)
        except IllegalOrderTransitionError:
            pass
        else:
            raise AssertionError("expected IllegalOrderTransitionError for FILLED -> FILLED")
