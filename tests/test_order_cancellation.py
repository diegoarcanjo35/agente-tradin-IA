"""Fase 2, item 7.3 / correção v1.1 #1: safe order cancellation --
`request_cancel()` is now fire-and-forget; confirmation of the real outcome
(CANCELLED, or a fill that won the race) always comes from a subsequent
`poll_order()` call -- there is no more blocking `cancel()` method.
"""
from __future__ import annotations

import pytest

from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.execution.idempotency import make_idempotency_key
from app.execution.order_state import OrderStatus
from app.execution.paper_local import PaperLocalExecutionEngine
from app.risk.engine import ApprovedOrder
from tests.factories import approved_open_order
from tests.fakes.bybit_fake import FakeBybitTransport


def make_order(qty=0.001) -> ApprovedOrder:
    return approved_open_order(
        symbol="BTCUSDT", side="BUY", qty=qty, price=40000.0,
        stop_loss=39000.0, take_profit=41000.0,
    )


def make_bybit_engine(transport, **kwargs) -> BybitDemoExecutionEngine:
    kwargs.setdefault("sleep", lambda s: None)
    return BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get, **kwargs
    )


def test_paper_local_request_cancel_is_a_no_op_poll_still_reports_the_terminal_fill():
    """PAPER_LOCAL orders fill synchronously inside submit() -- there is no
    non-terminal window, so request_cancel() is a documented no-op; a
    subsequent poll_order() still reports the real terminal state already
    computed, never a fabricated CANCELLED."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)

    engine.request_cancel(ack.exchange_order_id)  # no-op, must not raise
    snapshot = engine.poll_order(ack.exchange_order_id)
    assert snapshot.status == OrderStatus.FILLED
    assert snapshot.fills[0].fill_qty == pytest.approx(order.qty)


def test_paper_local_poll_of_unknown_order_id_after_cancel_reports_unknown():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0)
    engine.request_cancel("NEVER-SUBMITTED")  # no-op, must not raise
    snapshot = engine.poll_order("NEVER-SUBMITTED")
    assert snapshot.status == OrderStatus.UNKNOWN


def test_paper_local_list_open_orders_is_always_empty():
    """PAPER orders never stay open -- submit() always resolves them
    synchronously, so there is never an order the simulator considers open."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0)
    order = make_order()
    engine.submit(order, make_idempotency_key(order, "bucket-1"))
    assert engine.list_open_orders("BTCUSDT") == []


def test_bybit_demo_request_cancel_then_poll_confirms_cancelled():
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)

    engine.request_cancel(ack.exchange_order_id)
    cancel_call = [c for c in transport.post_calls if c[0].endswith("/v5/order/cancel")]
    assert len(cancel_call) == 1
    assert cancel_call[0][1]["orderId"] == ack.exchange_order_id

    transport.queue_status(ack.exchange_order_id, [{"orderStatus": "Cancelled"}])
    snapshot = engine.poll_order(ack.exchange_order_id)
    assert snapshot.status == OrderStatus.CANCELLED
    assert snapshot.fills == []


def test_bybit_demo_cancel_race_lost_to_fill_reports_the_real_fill_not_a_fake_cancel():
    """The exact race the correction requires handling: a cancel is
    requested, but the exchange reports the order already FILLED by the
    time the next poll_order() runs. Must report the real fill, never a
    fabricated CANCELLED."""
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)

    engine.request_cancel(ack.exchange_order_id)
    transport.queue_status(ack.exchange_order_id, [{"orderStatus": "Filled"}])
    transport.queue_executions(ack.exchange_order_id, [
        {"execId": "EXEC-1", "execQty": "0.001", "execPrice": "40010.0", "execFee": "0.024"},
    ])

    snapshot = engine.poll_order(ack.exchange_order_id)
    assert snapshot.status == OrderStatus.FILLED
    assert snapshot.fills[0].fill_qty == pytest.approx(0.001)
    assert snapshot.fills[0].fill_price == pytest.approx(40010.0)


def test_bybit_demo_poll_after_cancel_that_never_confirms_reports_unknown_not_cancelled():
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport)
    order_id = "EX-neverseen"
    engine.request_cancel(order_id)  # never raises even for an unknown id
    # No status queued at all -- polling finds nothing.
    snapshot = engine.poll_order(order_id)
    assert snapshot.status == OrderStatus.UNKNOWN


def test_bybit_demo_list_open_orders_reports_only_new_and_partially_filled():
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport)
    transport.set_open_orders("BTCUSDT", [
        {"orderId": "EX-1", "orderStatus": "New", "side": "Buy", "qty": "0.01"},
        {"orderId": "EX-2", "orderStatus": "PartiallyFilled", "side": "Sell", "qty": "0.02"},
        {"orderId": "EX-3", "orderStatus": "Filled", "side": "Buy", "qty": "0.03"},
    ])
    open_orders = engine.list_open_orders("BTCUSDT")
    assert {o["exchange_order_id"] for o in open_orders} == {"EX-1", "EX-2"}
