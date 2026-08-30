"""Covers spec section 7 items 13-16: submit/poll separation, Bybit timeout,
rate limit, and partial execution -- for both PAPER_LOCAL and BYBIT_DEMO
execution engines, using the no-network fake transport.

Correção v1.1 #1: `submit()` no longer blocks/confirms -- it only returns a
`SubmitAck` (SUBMITTED/REJECTED/UNKNOWN). Any fill only ever comes from a
separate `poll_order()` call, and engine-level `_seen_keys` idempotency was
removed entirely -- deduplication is now the caller's (orchestrator's)
responsibility, backed by the DB (see tests/test_reconciliation_periodic_and_order_lifecycle.py
and tests/test_price_correctness.py for that layer).
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


def test_paper_local_submit_returns_submitted_ack_then_poll_reports_fill_and_fee():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, fee_rate=0.001, slippage_bps=0)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")

    ack = engine.submit(order, key)
    assert ack.status == OrderStatus.SUBMITTED
    assert ack.exchange_order_id

    snapshot = engine.poll_order(ack.exchange_order_id)
    assert snapshot.status == OrderStatus.FILLED
    assert len(snapshot.fills) == 1
    fill = snapshot.fills[0]
    assert fill.exchange_fill_id
    assert fill.fill_price == pytest.approx(40000.0)
    assert fill.fee == pytest.approx(order.qty * 40000.0 * 0.001)


def test_paper_local_repolling_the_same_order_reports_the_same_fill_id():
    """poll_order() always serves the full known fill history -- polling
    twice must report the SAME exchange_fill_id both times (the fill
    ledger, not the engine, is what makes re-applying it a no-op)."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)

    first_poll = engine.poll_order(ack.exchange_order_id)
    second_poll = engine.poll_order(ack.exchange_order_id)
    assert first_poll.fills[0].exchange_fill_id == second_poll.fills[0].exchange_fill_id


def test_paper_local_partial_fill():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0, partial_fill_ratio=0.4)
    order = make_order(qty=1.0)
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)
    snapshot = engine.poll_order(ack.exchange_order_id)
    assert snapshot.status == OrderStatus.PARTIALLY_FILLED
    assert snapshot.fills[0].fill_qty == pytest.approx(0.4)


def test_paper_local_poll_of_unknown_order_id_reports_unknown():
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 40000.0)
    snapshot = engine.poll_order("NEVER-SUBMITTED")
    assert snapshot.status == OrderStatus.UNKNOWN
    assert snapshot.fills == []


def test_bybit_demo_timeout_on_submit_yields_unknown_ack():
    transport = FakeBybitTransport()
    transport.fail_next_n_with_timeout = 1
    engine = make_bybit_engine(transport)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)
    assert ack.status == OrderStatus.UNKNOWN


def test_bybit_demo_rate_limit_on_submit_yields_unknown_ack():
    transport = FakeBybitTransport()
    transport.fail_next_n_with_rate_limit = 1
    engine = make_bybit_engine(transport)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)
    assert ack.status == OrderStatus.UNKNOWN


def test_bybit_demo_create_rejected_without_order_id_yields_rejected_ack():
    transport = FakeBybitTransport()

    def broken_post(url, payload):
        return {"retCode": 0, "result": {}}  # no orderId

    transport.http_post = broken_post
    engine = make_bybit_engine(transport)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)
    assert ack.status == OrderStatus.REJECTED
    assert ack.exchange_order_id == ""


def test_bybit_demo_http_200_alone_is_not_treated_as_filled():
    """The create call succeeding must not be enough -- only poll_order()
    (never submit() itself) can ever report a fill."""
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)
    assert ack.status == OrderStatus.SUBMITTED  # accepted, not yet filled

    # No status queued -> a single poll finds nothing -> UNKNOWN, never a
    # fabricated FILLED, and never an internal retry loop (that's the
    # periodic poller's job now, not this method's).
    snapshot = engine.poll_order(ack.exchange_order_id)
    assert snapshot.status == OrderStatus.UNKNOWN
    assert snapshot.fills == []


def test_bybit_demo_confirms_fill_via_poll_order():
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)
    transport.queue_status(ack.exchange_order_id, [{"orderStatus": "Filled"}])
    transport.queue_executions(ack.exchange_order_id, [
        {"execId": "EXEC-1", "execQty": "0.001", "execPrice": "40010", "execFee": "0.02"},
    ])

    snapshot = engine.poll_order(ack.exchange_order_id)
    assert snapshot.status == OrderStatus.FILLED
    assert len(snapshot.fills) == 1
    assert snapshot.fills[0].fill_qty == pytest.approx(0.001)
    assert snapshot.fills[0].exchange_fill_id == "EXEC-1"


def test_bybit_demo_partial_fill_via_poll_order():
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport)
    order = make_order(qty=1.0)
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)
    transport.queue_status(ack.exchange_order_id, [{"orderStatus": "PartiallyFilled"}])
    transport.queue_executions(ack.exchange_order_id, [
        {"execId": "EXEC-1", "execQty": "0.4", "execPrice": "40010", "execFee": "0.01"},
    ])

    snapshot = engine.poll_order(ack.exchange_order_id)
    assert snapshot.status == OrderStatus.PARTIALLY_FILLED
    assert snapshot.fills[0].fill_qty == pytest.approx(0.4)


def test_bybit_demo_multiple_individual_fills_are_all_reported():
    """Real Bybit /v5/execution/list can report several individual fills
    for one order -- poll_order() must surface all of them, never just the
    latest/cumulative one."""
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport)
    order = make_order(qty=1.0)
    key = make_idempotency_key(order, "bucket-1")
    ack = engine.submit(order, key)
    transport.queue_status(ack.exchange_order_id, [{"orderStatus": "Filled"}])
    transport.queue_executions(ack.exchange_order_id, [
        {"execId": "EXEC-1", "execQty": "0.4", "execPrice": "40000", "execFee": "0.01"},
        {"execId": "EXEC-2", "execQty": "0.6", "execPrice": "40100", "execFee": "0.015"},
    ])

    snapshot = engine.poll_order(ack.exchange_order_id)
    assert {f.exchange_fill_id for f in snapshot.fills} == {"EXEC-1", "EXEC-2"}


def test_bybit_demo_create_posts_exactly_once_per_submit_call():
    transport = FakeBybitTransport()
    engine = make_bybit_engine(transport)
    order = make_order()
    key = make_idempotency_key(order, "bucket-1")
    engine.submit(order, key)
    create_calls = [c for c in transport.post_calls if c[0].endswith("/v5/order/create")]
    assert len(create_calls) == 1
