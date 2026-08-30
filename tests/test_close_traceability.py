"""Correction v1.1 #3: every close attempt must persist the full chain --
originating signal, risk evaluation, order, execution/fill, fees, position
update, and idempotency key -- with real foreign keys (never signal_id=0).
"""
from __future__ import annotations

from app.persistence import repo
from app.persistence.db import session_scope
from tests.test_stop_take_profit import build_orchestrator_with_open_position
from tests.test_price_correctness import make_candle


def test_stop_loss_close_persists_the_full_chain(session_factory):
    candle = make_candle(0, close=95)
    candle = candle.__class__(
        symbol="BTCUSDT", timeframe="1m", open_time=candle.open_time,
        open=95, high=96, low=85, close=95, volume=10, source="test", received_at=candle.received_at,
    )
    orch = build_orchestrator_with_open_position(session_factory, "BUY", 100.0, 90.0, 120.0, [candle])
    result = orch.tick()
    assert result["status"] == "position_closed"

    with session_scope(session_factory) as session:
        signals = repo.recent_signals(session, limit=10)
        close_signal = next(s for s in signals if s.direction == "SELL")
        assert close_signal.id != 0
        assert close_signal.id is not None

        risk_evals = repo.recent_risk_evaluations(session, limit=10)
        close_eval = next(r for r in risk_evals if r.signal_id == close_signal.id)
        assert close_eval.approved is True
        assert close_eval.signal_id == close_signal.id
        assert close_eval.signal_id != 0

        from sqlalchemy import select
        from app.persistence.models import Order, Execution, Position

        order = session.execute(
            select(Order).where(Order.risk_evaluation_id == close_eval.id)
        ).scalar_one()
        assert order.is_close is True
        assert order.risk_evaluation_id == close_eval.id
        assert order.idempotency_key  # non-empty, deterministic key was set
        assert order.status in ("FILLED", "PARTIALLY_FILLED")
        assert order.exchange_order_id

        execution = session.execute(
            select(Execution).where(Execution.order_id == order.id)
        ).scalar_one()
        assert execution.fee >= 0
        assert execution.fill_qty > 0

        position = session.execute(select(Position)).scalars().one()
        assert position.status == "CLOSED"
        assert position.fees_paid >= execution.fee
        assert position.closed_at is not None

        # Full chain, end to end:
        assert order.risk_evaluation_id == close_eval.id
        assert close_eval.signal_id == close_signal.id


def test_rejected_close_still_persists_signal_and_risk_evaluation(session_factory):
    """Even when a close is rejected (e.g. kill switch engaged), the
    originating signal and the risk evaluation that rejected it must still
    be persisted -- rejections are traceable too."""
    candle = make_candle(0, close=95)
    candle = candle.__class__(
        symbol="BTCUSDT", timeframe="1m", open_time=candle.open_time,
        open=95, high=96, low=85, close=95, volume=10, source="test", received_at=candle.received_at,
    )
    orch = build_orchestrator_with_open_position(session_factory, "BUY", 100.0, 90.0, 120.0, [candle])

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = True
        state.trading_blocked = True

    result = orch.tick()
    assert result["status"] == "close_rejected"

    with session_scope(session_factory) as session:
        signals = repo.recent_signals(session, limit=10)
        close_signal = next(s for s in signals if s.direction == "SELL")
        risk_evals = repo.recent_risk_evaluations(session, limit=10)
        close_eval = next(r for r in risk_evals if r.signal_id == close_signal.id)
        assert close_eval.approved is False

        positions = repo.open_positions(session)
        assert len(positions) == 1  # never closed
