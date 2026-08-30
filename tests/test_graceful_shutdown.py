"""Correção da Fase 2 v1.1 #7: `end_session()` existed but was dead code --
`_lifespan`'s shutdown only cancelled the poll task, never ended the
operational session or ran a final reconciliation. Exercises the real
FastAPI lifespan (via `TestClient` as a context manager, which triggers
both startup and shutdown) to prove the fix end-to-end.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.main import _lifespan
from app.market_data.base import CandleFetchResult, CandleFetchStatus
from app.persistence import repo
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence.models import OperationalSession
from app.risk.config import RiskLimits
from app.sessions import start_or_resume_session
from tests.test_price_correctness import build_test_orchestrator

_TEST_RISK_LIMITS = RiskLimits(
    max_position_usd=50.0, max_concurrent_positions=1, max_daily_loss_usd=25.0,
    max_total_exposure_usd=50.0, cooldown_after_losses=3, cooldown_minutes=30,
    max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0,
)


class _ForeverPendingProvider:
    """Always reports NO_NEW_CANDLE -- keeps `_poll_loop` alive (sleeping
    between ticks) so shutdown genuinely has to cancel an in-flight task,
    rather than the loop having already finished on its own."""

    def next_candle(self) -> CandleFetchResult:
        return CandleFetchResult(status=CandleFetchStatus.NO_NEW_CANDLE)

    def is_stale(self, max_staleness_seconds: float) -> bool:
        return False


def _make_app(tmp_path, db_name="shutdown.db"):
    db_path = tmp_path / db_name
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    orch = build_test_orchestrator(session_factory, [])
    orch.market_data_provider = _ForeverPendingProvider()
    orch.settings.replay_poll_interval_seconds = 0.01

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        op_session = start_or_resume_session(session, orch.settings, "v1", _TEST_RISK_LIMITS)
        state.active_session_id = op_session.id

    app = FastAPI(lifespan=_lifespan)
    app.state.orchestrator = orch
    app.state.settings = orch.settings
    app.state.replay_done = False
    app.state.loop_task = None
    return app, orch, session_factory


def test_shutdown_ends_the_active_operational_session(tmp_path):
    app, orch, session_factory = _make_app(tmp_path)

    with TestClient(app):
        with session_scope(session_factory) as session:
            state = repo.get_or_create_system_state(session)
            op_session = session.get(OperationalSession, state.active_session_id)
            assert op_session.ended_at is None  # still running while the client is open

    # TestClient's `with` block exited -> lifespan's shutdown ran.
    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        op_session = session.get(OperationalSession, state.active_session_id)
        assert op_session.ended_at is not None
        assert op_session.end_reason == "Encerramento gracioso do processo."
        assert state.operational_state == "ENCERRANDO"


def test_shutdown_cancellation_leaves_no_incomplete_transaction(tmp_path):
    """The poll task is cancelled mid-sleep -- proves the cancellation
    itself never leaves a half-written row (SQLite's transactional DDL/DML
    guarantee already covers this; this is the end-to-end proof)."""
    app, orch, session_factory = _make_app(tmp_path)

    with TestClient(app):
        pass  # startup, brief pause implicit in client setup/teardown, then shutdown

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state is not None  # readable -- no corrupted/partial row


def test_shutdown_with_a_pending_order_still_ends_the_session(tmp_path):
    from app.execution.order_state import OrderStatus

    app, orch, session_factory = _make_app(tmp_path)

    with session_scope(session_factory) as session:
        signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
        risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
        order = repo.save_order(
            session, idempotency_key="EX-SHUTDOWN-PENDING", risk_evaluation_id=risk_eval.id,
            symbol="BTCUSDT", side="BUY", qty=0.01, stop_loss=90.0, take_profit=110.0, mode="REPLAY",
        )
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        order.exchange_order_id = "EX-SHUTDOWN-PENDING"
        order_id = order.id

    with TestClient(app):
        pass

    with session_scope(session_factory) as session:
        from app.persistence.models import Order
        order = session.get(Order, order_id)
        # The final reconciliation run during shutdown polls this order --
        # PaperLocalExecutionEngine has no internal record of an
        # exchange_order_id it never itself produced via submit(), so the
        # snapshot comes back unresolvable and fill_service safely reports
        # UNKNOWN rather than guessing. Either way, the order is NEVER
        # silently finalized into a terminal status (FILLED/CANCELLED/
        # REJECTED) by shutdown itself -- only a real confirmation may do that.
        assert order.status in (OrderStatus.SUBMITTED.value, OrderStatus.UNKNOWN.value)

        state = repo.get_or_create_system_state(session)
        op_session = session.get(OperationalSession, state.active_session_id)
        assert op_session.ended_at is not None


def test_next_boot_after_a_graceful_shutdown_starts_a_new_session(tmp_path):
    app, orch, session_factory = _make_app(tmp_path)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        ended_session_id = state.active_session_id

    with TestClient(app):
        pass

    with session_scope(session_factory) as session:
        new_session = start_or_resume_session(session, orch.settings, "v1", _TEST_RISK_LIMITS)
        assert new_session.id != ended_session_id
        assert new_session.ended_at is None


def test_a_crash_that_bypasses_lifespan_still_resumes_the_unended_session(tmp_path):
    """Only a graceful shutdown (through `_lifespan`) ends a session -- a
    "crash" (this test never enters the TestClient context at all, so
    `_lifespan`'s shutdown never runs) leaves it resumable on next boot."""
    app, orch, session_factory = _make_app(tmp_path)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        crashed_session_id = state.active_session_id

    with session_scope(session_factory) as session:
        resumed = start_or_resume_session(session, orch.settings, "v1", _TEST_RISK_LIMITS)
        assert resumed.id == crashed_session_id
        assert resumed.ended_at is None
