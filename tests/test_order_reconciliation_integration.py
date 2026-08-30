"""Correção da Fase 2 v1.1 #3: `Orchestrator.reconcile()` now reconciles
orders, not just positions -- an order the exchange has that isn't tracked
locally, a locally non-terminal order the exchange no longer reports as
open, and a fill missed by the periodic poller and recovered deterministically
through the exact same `fill_service.apply_order_snapshot` path used
everywhere else. Also proves item 7: a failure in one half (positions vs.
orders) never masks -- and a clean half never silently clears -- a
divergence found by the other half.
"""
from __future__ import annotations

from app.execution.base import FillEvent, OrderStatusSnapshot
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Order, Position
from tests.test_price_correctness import build_test_orchestrator


def _make_non_terminal_order(orch, exchange_order_id: str, status: OrderStatus, symbol="BTCUSDT") -> int:
    with session_scope(orch.session_factory) as session:
        signal = repo.save_signal(session, symbol, "BUY", "teste", 100.0, 1.0, {})
        risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
        order = repo.save_order(
            session, idempotency_key=exchange_order_id, risk_evaluation_id=risk_eval.id,
            symbol=symbol, side="BUY", qty=0.01, stop_loss=90.0, take_profit=110.0, mode="REPLAY",
        )
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        order.exchange_order_id = exchange_order_id
        if status != OrderStatus.SUBMITTED:
            repo.transition_order_status(session, order, status)
        order_id = order.id
    return order_id


def test_reconcile_detects_unknown_remote_order_and_blocks_trading(session_factory):
    orch = build_test_orchestrator(session_factory, [])
    orch.execution_engine.list_open_orders = lambda symbol: (
        [{"exchange_order_id": "EX-GHOST", "side": "BUY", "qty": 0.01}] if symbol == "BTCUSDT" else []
    )

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True
        assert state.reconciliation_diverged is True
        failures = repo.recent_failures(session, limit=10)
        assert any(
            f.kind == "RECONCILIATION" and not f.resolved and f.mismatches_json and "EX-GHOST" in f.mismatches_json
            for f in failures
        )


def test_reconcile_detects_locally_tracked_order_missing_remotely(session_factory):
    orch = build_test_orchestrator(session_factory, [])
    order_id = _make_non_terminal_order(orch, "EX-MISSING", OrderStatus.SUBMITTED)

    orch.execution_engine.poll_order = lambda exchange_order_id: OrderStatusSnapshot(
        exchange_order_id=exchange_order_id, status=OrderStatus.SUBMITTED, fills=[],
    )
    orch.execution_engine.list_open_orders = lambda symbol: []

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True
        assert state.reconciliation_diverged is True
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTED.value  # never guessed/auto-changed


def test_reconcile_recovers_a_missed_fill_deterministically(session_factory):
    """A fill the periodic poller never got a chance to see (e.g. the
    process only just restarted) is recovered by `reconcile()` itself, via
    the same `fill_service.apply_order_snapshot` path -- never a
    second/divergent code path."""
    orch = build_test_orchestrator(session_factory, [])
    order_id = _make_non_terminal_order(orch, "EX-RECOVER", OrderStatus.SUBMITTED)

    orch.execution_engine.poll_order = lambda exchange_order_id: OrderStatusSnapshot(
        exchange_order_id=exchange_order_id, status=OrderStatus.FILLED,
        fills=[FillEvent(exchange_fill_id="FILL-1", fill_qty=0.01, fill_price=100.0, fee=0.01)],
    )
    orch.execution_engine.list_open_orders = lambda symbol: []

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value
        assert order.filled_qty == 0.01
        positions = repo.open_positions(session, "BTCUSDT")
        assert len(positions) == 1
        assert positions[0].qty == 0.01


def test_reconcile_reported_fill_twice_is_a_safe_no_op(session_factory):
    """Correção v1.1 #2: the same `exchange_fill_id` reported across two
    reconcile() runs (poll_order always reports the FULL fill history, per
    contract) must never be applied twice."""
    orch = build_test_orchestrator(session_factory, [])
    order_id = _make_non_terminal_order(orch, "EX-DUP", OrderStatus.SUBMITTED)

    orch.execution_engine.poll_order = lambda exchange_order_id: OrderStatusSnapshot(
        exchange_order_id=exchange_order_id, status=OrderStatus.FILLED,
        fills=[FillEvent(exchange_fill_id="FILL-DUP", fill_qty=0.01, fill_price=100.0, fee=0.01)],
    )
    orch.execution_engine.list_open_orders = lambda symbol: []

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)
    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)  # same fill reported again -- must be a no-op

    with session_scope(session_factory) as session:
        order = session.get(Order, order_id)
        assert order.filled_qty == 0.01  # not 0.02
        positions = repo.open_positions(session, "BTCUSDT")
        assert len(positions) == 1
        assert positions[0].qty == 0.01


def test_reconcile_covers_orders_across_multiple_symbols(session_factory):
    orch = build_test_orchestrator(session_factory, [])
    _make_non_terminal_order(orch, "EX-BTC", OrderStatus.SUBMITTED, symbol="BTCUSDT")
    _make_non_terminal_order(orch, "EX-ETH", OrderStatus.SUBMITTED, symbol="ETHUSDT")

    polled_ids = []

    def fake_poll(exchange_order_id):
        polled_ids.append(exchange_order_id)
        return OrderStatusSnapshot(exchange_order_id=exchange_order_id, status=OrderStatus.SUBMITTED, fills=[])

    queried_symbols = []

    def fake_list_open(symbol):
        queried_symbols.append(symbol)
        if symbol == "BTCUSDT":
            return [{"exchange_order_id": "EX-BTC", "side": "BUY", "qty": 0.01}]
        if symbol == "ETHUSDT":
            return [{"exchange_order_id": "EX-ETH", "side": "BUY", "qty": 0.01}]
        return []

    orch.execution_engine.poll_order = fake_poll
    orch.execution_engine.list_open_orders = fake_list_open

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    assert set(polled_ids) == {"EX-BTC", "EX-ETH"}
    assert set(queried_symbols) == {"BTCUSDT", "ETHUSDT"}

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.reconciliation_diverged is False
        assert state.trading_blocked is False


def test_reconcile_order_failure_does_not_mask_a_position_divergence_already_found(session_factory):
    """Correção v1.1 #3 item 7: a failure reaching the exchange for the
    ORDER half must not clear the divergence the POSITION half already
    found (and vice versa) -- the combined result stays blocked either
    way."""
    orch = build_test_orchestrator(session_factory, [])

    with session_scope(session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 100.0, 90.0, 120.0)  # local-only position

    def raising_list_open_orders(symbol):
        raise RuntimeError("simulated network failure (orders)")

    orch.execution_engine.list_open_orders = raising_list_open_orders

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.reconciliation_diverged is True
        assert state.trading_blocked is True
        failures = repo.recent_failures(session, limit=10)
        kinds = [f for f in failures if f.kind == "RECONCILIATION"]
        # Both halves left their own record -- the order-half failure and
        # the position-half divergence are both independently visible.
        assert any("posições" in f.detail.lower() or "exchange reports none" in f.detail for f in kinds)
        assert any("ordens" in f.detail.lower() for f in kinds)


def test_reconcile_position_ok_does_not_clear_a_divergence_found_by_orders(session_factory):
    """Mirror of the above: positions are clean, but an unknown remote
    order keeps the system blocked -- a clean position half never silently
    clears a divergence the order half found."""
    orch = build_test_orchestrator(session_factory, [])
    orch.execution_engine.list_open_orders = lambda symbol: (
        [{"exchange_order_id": "EX-GHOST-2", "side": "BUY", "qty": 0.01}] if symbol == "BTCUSDT" else []
    )

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)  # no local/remote positions -> position half is clean

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.reconciliation_diverged is True
        assert state.trading_blocked is True
