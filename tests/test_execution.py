"""Covers spec section 7 items 13-16: duplicate order suppression, Bybit
timeout, rate limit, and partial execution -- for both PAPER_LOCAL and
BYBIT_DEMO execution engines, using the no-network fake transport.
"""
from __future__ import annotations

import pytest

from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.execution.idempotency import make_idempotency_key
from app.execution.paper_local import PaperLocalExecutionEngine
from app.risk.engine import ApprovedOrder, _RiskApprovalToken
from tests.fakes.bybit_fake import FakeBybitTransport


def make_order(qty=0.001) -> ApprovedOrder:
    return ApprovedOrder(
        signal_id=1, symbol="BTCUSDT", side="BUY", qty=qty,
        stop_loss=39000.0, take_profit=41000.0, token=_RiskApprovalToken(),
    )


def test_paper_local_fills_and_computes_fee():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, fee_rate=0.001, slippage_bps=0)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    result = engine.submit(order, key)
    assert result.status == "FILLED"
    assert result.fill_price == pytest.approx(40000.0)
    assert result.fee == pytest.approx(order.qty * 40000.0 * 0.001)


def test_paper_local_duplicate_order_is_suppressed_not_double_filled():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    first = engine.submit(order, key)
    second = engine.submit(order, key)
    assert first.exchange_order_id == second.exchange_order_id
    pos = engine.get_position(order.symbol)
    assert pos["qty"] == pytest.approx(order.qty)  # not doubled


def test_paper_local_partial_fill():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, partial_fill_ratio=0.4)
    order = make_order(qty=1.0)
    key = make_idempotency_key(order, "bucket-1")
    result = engine.submit(order, key)
    assert result.status == "PARTIALLY_FILLED"
    assert result.is_partial
    assert result.fill_qty == pytest.approx(0.4)


def test_bybit_demo_timeout_on_submit_does_not_crash_and_yields_error_status():
    transport = FakeBybitTransport()
    transport.fail_next_n_with_timeout = 1
    engine = BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get
    )
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    result = engine.submit(order, key)
    assert result.status == "ERROR"


def test_bybit_demo_rate_limit_on_submit_yields_error_status():
    transport = FakeBybitTransport()
    transport.fail_next_n_with_rate_limit = 1
    engine = BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get
    )
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    result = engine.submit(order, key)
    assert result.status == "ERROR"


def test_bybit_demo_http_200_alone_is_not_treated_as_executed():
    """The create call succeeding must not be enough -- submit() must poll
    order status and only report FILLED once the exchange confirms it."""
    transport = FakeBybitTransport()
    engine = BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get, max_status_polls=2
    )
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    order_id = f"EX-{key[:12]}"
    # No status queued -> polling finds nothing -> must NOT report FILLED.
    result = engine.submit(order, key)
    assert result.status == "ERROR"
    assert result.fill_qty == 0.0


def test_bybit_demo_confirms_fill_via_status_poll():
    transport = FakeBybitTransport()
    engine = BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get, max_status_polls=2
    )
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    order_id = f"EX-{key[:12]}"
    transport.queue_status(order_id, [
        {"orderStatus": "Filled", "cumExecQty": "0.001", "avgPrice": "40010", "cumExecFee": "0.02"}
    ])
    result = engine.submit(order, key)
    assert result.status == "FILLED"
    assert result.fill_qty == pytest.approx(0.001)


def test_bybit_demo_partial_fill_via_status_poll():
    transport = FakeBybitTransport()
    engine = BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get, max_status_polls=2
    )
    order = make_order(qty=1.0)
    key = make_idempotency_key(order, "bucket-1")
    order_id = f"EX-{key[:12]}"
    transport.queue_status(order_id, [
        {"orderStatus": "PartiallyFilled", "cumExecQty": "0.4", "avgPrice": "40010", "cumExecFee": "0.01"}
    ])
    result = engine.submit(order, key)
    assert result.status == "PARTIALLY_FILLED"
    assert result.is_partial


def test_bybit_demo_duplicate_order_suppressed():
    transport = FakeBybitTransport()
    engine = BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get, max_status_polls=2
    )
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    order_id = f"EX-{key[:12]}"
    transport.queue_status(order_id, [
        {"orderStatus": "Filled", "cumExecQty": "0.001", "avgPrice": "40010", "cumExecFee": "0.02"}
    ])
    first = engine.submit(order, key)
    second = engine.submit(order, key)
    assert first == second
    assert len(transport.post_calls) == 1
