"""Correction v1.1 #8: reconciliation must be integrated into the running
system, not just exist as an isolated pure function -- runs at orchestrator
construction (see test_bybit_demo_wiring.py), and again whenever an order
submission ends in an ERROR/unresolved status. Any mismatch blocks trading.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.persistence import repo
from app.persistence.db import session_scope
from app.risk.engine import RiskEngine
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_price_correctness import build_test_orchestrator


def test_reconcile_detects_local_only_position_and_blocks_trading(session_factory):
    orch = build_test_orchestrator(session_factory, [])

    with session_scope(session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 100.0, 90.0, 120.0)
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.state_ambiguous is True
        assert state.trading_blocked is True
        assert "RECONCILIATION_MISMATCH" in (state.block_reason or "")
        failures = repo.recent_failures(session, limit=10)
        assert any(f.kind == "RECONCILIATION" and not f.resolved for f in failures)
        events = repo.recent_security_events(session, limit=10)
        assert any(e.event_type == "RECONCILIATION_MISMATCH" for e in events)


def test_reconcile_ok_clears_prior_ambiguity(session_factory):
    orch = build_test_orchestrator(session_factory, [])

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.state_ambiguous = True
        state.trading_blocked = True
        state.block_reason = "RECONCILIATION_MISMATCH: stale from a previous run"
        orch.reconcile(session, state)  # no local positions, no remote -> matches

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.state_ambiguous is False
        assert state.trading_blocked is False
        assert state.block_reason is None


def test_reconcile_does_not_clear_kill_switch_block():
    """A block caused by the kill switch must never be silently lifted by a
    successful reconciliation -- only RECONCILIATION_MISMATCH blocks are
    auto-cleared."""
    from tests.test_price_correctness import build_test_orchestrator as _build

    def run(session_factory):
        orch = _build(session_factory, [])
        with session_scope(session_factory) as session:
            state = repo.get_or_create_system_state(session)
            state.kill_switch_engaged = True
            state.trading_blocked = True
            state.block_reason = "Kill switch engaged manually via dashboard."
            orch.reconcile(session, state)
        with session_scope(session_factory) as session:
            state = repo.get_or_create_system_state(session)
            assert state.trading_blocked is True
            assert state.block_reason == "Kill switch engaged manually via dashboard."

    from app.persistence.db import init_db, make_engine, make_session_factory
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    run(make_session_factory(engine))


def test_reconcile_blocks_when_exchange_query_itself_fails(session_factory):
    orch = build_test_orchestrator(session_factory, [])

    def raising_get_position(symbol):
        raise RuntimeError("simulated network failure")

    orch.execution_engine.get_position = raising_get_position

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.state_ambiguous is True
        assert state.trading_blocked is True
        events = repo.recent_security_events(session, limit=10)
        assert any(e.event_type == "RECONCILIATION_FAILED" for e in events)


def test_reconcile_runs_automatically_after_order_ends_in_error(session_factory):
    transport = FakeBybitTransport()
    transport.fail_next_n_with_timeout = 1
    execution_engine = BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get,
        sleep=lambda s: None,
    )

    orch = build_test_orchestrator(session_factory, [])
    orch.execution_engine = execution_engine

    approved = RiskEngine.make_test_approved_order(
        signal_id=1, symbol="BTCUSDT", side="BUY", qty=0.01, stop_loss=90.0, take_profit=110.0,
    )

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        risk_row = repo.save_risk_evaluation(session, 1, True, "approved for test", {})
        result = orch._submit_and_record(
            session, state, risk_row.id, approved, datetime(2024, 1, 1, tzinfo=timezone.utc), 100.0,
        )

    assert result["status"] == "order_not_filled"

    with session_scope(session_factory) as session:
        failures = repo.recent_failures(session, limit=10)
        assert any(f.kind == "RECONCILIATION" for f in failures)
        assert any(f.kind == "FAILURE" for f in failures)
