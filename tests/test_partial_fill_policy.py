"""Correção da Fase 2 v1.1 #2/#5: partial-fill policy -- WAIT (default)
never times out; CANCEL_REMAINDER and EXPIRE_AND_CANCEL both request
cancellation of the unfilled remainder once an order has sat
PARTIALLY_FILLED longer than PARTIAL_FILL_TIMEOUT_SECONDS.
"""
from __future__ import annotations

from datetime import timedelta

from app.api.main import build_orchestrator
from app.core.clock import utcnow
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Order
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import make_bybit_demo_settings


def _make_partially_filled_order(orch, exchange_order_id: str, stalled_seconds: float) -> int:
    with session_scope(orch.session_factory) as session:
        signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
        risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
        order = repo.save_order(
            session, idempotency_key=exchange_order_id, risk_evaluation_id=risk_eval.id,
            symbol="BTCUSDT", side="BUY", qty=1.0, stop_loss=90.0, take_profit=110.0, mode="BYBIT_DEMO",
        )
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        order.exchange_order_id = exchange_order_id
        repo.transition_order_status(session, order, OrderStatus.PARTIALLY_FILLED)
        order.filled_qty = 0.4
        order_id = order.id
        # Backdate updated_at so the order looks like it has been sitting
        # PARTIALLY_FILLED for `stalled_seconds` already.
        session.flush()
        backdated = (utcnow() - timedelta(seconds=stalled_seconds)).replace(tzinfo=None)
        session.execute(
            __import__("sqlalchemy").text("UPDATE orders SET updated_at = :t WHERE id = :id"),
            {"t": backdated.isoformat(sep=" "), "id": order_id},
        )
    return order_id


def test_wait_policy_never_cancels_regardless_of_how_long_it_has_stalled(tmp_path):
    settings = make_bybit_demo_settings(
        partial_fill_policy="WAIT", partial_fill_timeout_seconds=10.0,
        database_url=f"sqlite:///{tmp_path / 'wait_policy.db'}",
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    order_id = _make_partially_filled_order(orch, "EX-WAIT-1", stalled_seconds=99999.0)
    transport.queue_status("EX-WAIT-1", [{"orderStatus": "PartiallyFilled"}])

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_open_order_poll_at = None
        orch._maybe_poll_open_orders(session, state)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.PARTIALLY_FILLED.value  # never touched
    cancel_calls = [c for c in transport.post_calls if c[0].endswith("/v5/order/cancel")]
    assert cancel_calls == []


def test_cancel_remainder_policy_cancels_after_timeout_elapses(tmp_path):
    settings = make_bybit_demo_settings(
        partial_fill_policy="CANCEL_REMAINDER", partial_fill_timeout_seconds=10.0,
        database_url=f"sqlite:///{tmp_path / 'cancel_remainder.db'}",
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    order_id = _make_partially_filled_order(orch, "EX-CR-1", stalled_seconds=60.0)  # well past the 10s timeout
    transport.queue_status("EX-CR-1", [
        {"orderStatus": "PartiallyFilled"},  # first poll (before the timeout check): no status change
        {"orderStatus": "Cancelled"},  # second poll, after request_cancel() fires
    ])

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_open_order_poll_at = None
        orch._maybe_poll_open_orders(session, state)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.CANCELLED.value
    cancel_calls = [c for c in transport.post_calls if c[0].endswith("/v5/order/cancel")]
    assert len(cancel_calls) == 1
    assert cancel_calls[0][1]["orderId"] == "EX-CR-1"


def test_cancel_remainder_policy_does_not_cancel_before_timeout_elapses(tmp_path):
    settings = make_bybit_demo_settings(
        partial_fill_policy="CANCEL_REMAINDER", partial_fill_timeout_seconds=300.0,
        database_url=f"sqlite:///{tmp_path / 'cancel_remainder_early.db'}",
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    order_id = _make_partially_filled_order(orch, "EX-CR-2", stalled_seconds=5.0)  # well under the 300s timeout
    transport.queue_status("EX-CR-2", [{"orderStatus": "PartiallyFilled"}])

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_open_order_poll_at = None
        orch._maybe_poll_open_orders(session, state)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.PARTIALLY_FILLED.value
    cancel_calls = [c for c in transport.post_calls if c[0].endswith("/v5/order/cancel")]
    assert cancel_calls == []


def test_expire_and_cancel_policy_cancels_after_timeout_elapses(tmp_path):
    settings = make_bybit_demo_settings(
        partial_fill_policy="EXPIRE_AND_CANCEL", partial_fill_timeout_seconds=10.0,
        database_url=f"sqlite:///{tmp_path / 'expire_and_cancel.db'}",
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    order_id = _make_partially_filled_order(orch, "EX-EAC-1", stalled_seconds=60.0)
    transport.queue_status("EX-EAC-1", [
        {"orderStatus": "PartiallyFilled"},
        {"orderStatus": "Cancelled"},
    ])

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_open_order_poll_at = None
        orch._maybe_poll_open_orders(session, state)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.CANCELLED.value


def test_partial_fill_policy_rejected_at_settings_construction_when_invalid():
    import pytest

    from app.core.config import Settings

    with pytest.raises(Exception):
        Settings(partial_fill_policy="NOT_A_REAL_POLICY")
