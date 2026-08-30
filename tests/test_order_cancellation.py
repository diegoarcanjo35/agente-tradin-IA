"""Fase 2, item 7.3: safe order cancellation -- only non-terminal orders,
idempotent, always confirmed on the exchange, and correctly handling the
race where a fill wins before the cancel takes effect.
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


def test_paper_local_cancel_reports_the_already_terminal_fill_state():
    """PAPER_LOCAL orders fill synchronously inside submit() -- there is no
    non-terminal window, so cancel() is a documented no-op that reports the
    real terminal state already recorded, never a fabricated CANCELLED."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    fill = engine.submit(order, key)

    result = engine.cancel(fill.exchange_order_id)
    assert result.status == OrderStatus.FILLED
    assert result.filled_qty == pytest.approx(fill.fill_qty)


def test_paper_local_cancel_of_unknown_order_id_reports_unknown():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0)
    result = engine.cancel("NEVER-SUBMITTED")
    assert result.status == OrderStatus.UNKNOWN


def test_bybit_demo_cancel_confirms_cancelled_via_status_poll():
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport, max_status_polls=2)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    order_id = f"EX-{key[:12]}"
    transport.queue_status(order_id, [{"orderStatus": "New"}])  # submit() confirm poll: still open

    submit_result = engine.submit(order, key)
    assert submit_result.status == OrderStatus.UNKNOWN  # never confirmed filled -- still open

    transport.queue_status(order_id, [{"orderStatus": "Cancelled"}])
    cancel_result = engine.cancel(order_id)
    assert cancel_result.status == OrderStatus.CANCELLED

    cancel_call = [c for c in transport.post_calls if c[0].endswith("/v5/order/cancel")]
    assert len(cancel_call) == 1
    assert cancel_call[0][1]["orderId"] == order_id


def test_bybit_demo_cancel_race_lost_to_fill_reports_the_real_fill_not_a_fake_cancel():
    """The exact race the correction requires handling: a cancel is
    requested, but the exchange reports the order already FILLED by the
    time the confirmation poll runs. cancel() must report the real fill,
    never a fabricated CANCELLED."""
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport, max_status_polls=2)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    order_id = f"EX-{key[:12]}"
    transport.queue_status(order_id, [{"orderStatus": "New"}])
    engine.submit(order, key)

    transport.queue_status(order_id, [{
        "orderStatus": "Filled", "cumExecQty": "0.001", "avgPrice": "40010.0", "cumExecFee": "0.024",
    }])
    result = engine.cancel(order_id)
    assert result.status == OrderStatus.FILLED
    assert result.filled_qty == pytest.approx(0.001)
    assert result.avg_fill_price == pytest.approx(40010.0)


def test_bybit_demo_cancel_that_never_confirms_reports_unknown_not_cancelled():
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport, max_status_polls=2)
    order_id = "EX-neverseen"
    # No status queued at all -- polling finds nothing every time.
    result = engine.cancel(order_id)
    assert result.status == OrderStatus.UNKNOWN
