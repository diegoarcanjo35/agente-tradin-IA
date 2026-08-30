"""Correção da Fase 2 v1.2 #4: `BybitDemoExecutionEngine.list_open_orders()`
used to catch a transport failure and return `[]` -- with no local open
orders, that made a reconciliation that NEVER REACHED THE EXCHANGE look
clean. Now every transport failure or broken pagination contract raises
(never an empty list standing in for "couldn't check"), and
`Orchestrator._reconcile_orders_step` already treats any exception as a
failed verification -- this file proves the fix end-to-end, plus the
requirement that a later clean reconciliation only clears the flags IT
owns, never an unrelated block cause.
"""
from __future__ import annotations

from app.core.errors import ExchangeDataIncompleteError, ExchangeTimeoutError, RateLimitError
from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.persistence import repo
from app.persistence.db import session_scope
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_price_correctness import build_test_orchestrator


def test_no_local_orders_but_remote_timeout_is_not_a_clean_reconciliation(session_factory):
    orch = build_test_orchestrator(session_factory, [])
    orch.execution_engine.list_open_orders = _raise(ExchangeTimeoutError("timeout simulado"))

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True
        assert state.reconciliation_diverged is True
        events = repo.recent_security_events(session, limit=10)
        assert any(e.event_type == "RECONCILIATION_FAILED" for e in events)


def test_rate_limit_on_open_orders_query_keeps_blocked(session_factory):
    orch = build_test_orchestrator(session_factory, [])
    orch.execution_engine.list_open_orders = _raise(RateLimitError("rate limit simulado"))

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True
        assert state.reconciliation_diverged is True


def test_malformed_payload_on_open_orders_query_keeps_blocked(tmp_path):
    from app.api.main import build_orchestrator
    from tests.test_bybit_demo_wiring import make_bybit_demo_settings

    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'malformed_open_orders.db'}")
    transport = FakeBybitTransport()
    # build_orchestrator() itself runs a startup reconciliation -- queue the
    # malformed page for THAT call, then assert on its outcome directly
    # (queue_open_orders_pages exhausted afterward would otherwise fall back
    # to a valid empty page, masking the scenario on a second call).
    transport.queue_open_orders_pages("BTCUSDT", ["MALFORMED"])
    orch = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True
        assert state.reconciliation_diverged is True


def test_failure_on_a_later_page_of_open_orders_keeps_blocked(tmp_path):
    from app.api.main import build_orchestrator
    from tests.test_bybit_demo_wiring import make_bybit_demo_settings

    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'later_page_open_orders.db'}")
    transport = FakeBybitTransport()
    transport.queue_open_orders_pages("BTCUSDT", [
        {"list": [{"orderId": "EX-1", "orderStatus": "New", "side": "Buy", "qty": "0.01"}],
         "nextPageCursor": "cursor-1"},
        ExchangeTimeoutError("timeout na página seguinte"),
    ])
    orch = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True
        assert state.reconciliation_diverged is True


def test_genuinely_empty_valid_list_permits_a_clean_result(tmp_path):
    from app.api.main import build_orchestrator
    from tests.test_bybit_demo_wiring import make_bybit_demo_settings

    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'genuinely_empty.db'}")
    transport = FakeBybitTransport()
    transport.queue_open_orders_pages("BTCUSDT", [{"list": []}])  # one real, complete, empty page
    orch = build_orchestrator(settings, bybit_transport=transport)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.reconciliation_diverged is False
        assert state.trading_blocked is False


def test_a_later_clean_reconciliation_only_clears_flags_it_owns(session_factory):
    """A reconciliation clean on both positions and orders must NEVER
    silently clear an unrelated block cause (e.g. the kill switch) --
    trading_blocked is derived from the union of every independent cause."""
    orch = build_test_orchestrator(session_factory, [])

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = True
        repo.recompute_trading_blocked(state, orch.settings.risk_max_api_failures)
        orch.reconcile(session, state)  # no local positions/orders, no remote -> clean

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.reconciliation_diverged is False  # reconciliation's OWN cause is clear
        assert state.trading_blocked is True  # but the kill switch cause is untouched
        assert "emergência" in (state.block_reason or "").lower()


def _raise(exc: Exception):
    def _fn(symbol):
        raise exc
    return _fn
