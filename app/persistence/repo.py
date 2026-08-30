"""Thin typed repository helpers over a SQLAlchemy Session. Kept intentionally
simple (no repository-pattern abstraction beyond this) since the app is small
enough that a query-builder layer would be premature.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.persistence.models import (
    AccountSnapshot,
    AIRecommendation,
    Candle,
    Execution,
    FailureReconciliation,
    Order,
    Position,
    RiskEvaluation,
    SecurityEvent,
    StrategySignal,
    SystemState,
)


def get_or_create_system_state(session: Session) -> SystemState:
    state = session.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1)
        session.add(state)
        session.flush()
    return state


def record_security_event(session: Session, event_type: str, detail: str) -> SecurityEvent:
    ev = SecurityEvent(event_type=event_type, detail=detail)
    session.add(ev)
    session.flush()
    return ev


def record_failure(session: Session, kind: str, detail: str) -> FailureReconciliation:
    fr = FailureReconciliation(kind=kind, detail=detail)
    session.add(fr)
    session.flush()
    return fr


def save_candle(session: Session, symbol: str, timeframe: str, open_time: datetime,
                 open_: float, high: float, low: float, close: float, volume: float,
                 source: str) -> Candle:
    c = Candle(
        symbol=symbol, timeframe=timeframe, open_time=open_time,
        open=open_, high=high, low=low, close=close, volume=volume, source=source,
    )
    session.add(c)
    session.flush()
    return c


def save_signal(session: Session, symbol: str, direction: str, justification: str,
                 observed_price: float, atr: float, params: dict) -> StrategySignal:
    s = StrategySignal(
        symbol=symbol, direction=direction, justification=justification,
        observed_price=observed_price, atr=atr, params_json=json.dumps(params),
    )
    session.add(s)
    session.flush()
    return s


def save_ai_recommendation(session: Session, symbol: str, signal_id: int | None,
                            recommendation: str, confidence: float, reasoning_summary: str,
                            risk_flags: list[str], provider: str, model_version: str,
                            is_valid: bool, rejection_reason: str | None) -> AIRecommendation:
    rec = AIRecommendation(
        symbol=symbol, signal_id=signal_id, recommendation=recommendation,
        confidence=confidence, reasoning_summary=reasoning_summary,
        risk_flags_json=json.dumps(risk_flags), provider=provider,
        model_version=model_version, is_valid=is_valid, rejection_reason=rejection_reason,
    )
    session.add(rec)
    session.flush()
    return rec


def save_risk_evaluation(session: Session, signal_id: int, approved: bool, reason: str,
                          checks: dict) -> RiskEvaluation:
    ev = RiskEvaluation(
        signal_id=signal_id, approved=approved, reason=reason, checks_json=json.dumps(checks),
    )
    session.add(ev)
    session.flush()
    return ev


def find_order_by_idempotency_key(session: Session, key: str) -> Order | None:
    return session.execute(select(Order).where(Order.idempotency_key == key)).scalar_one_or_none()


def save_order(session: Session, idempotency_key: str, risk_evaluation_id: int, symbol: str,
               side: str, qty: float, stop_loss: float, take_profit: float | None,
               mode: str) -> Order:
    o = Order(
        idempotency_key=idempotency_key, risk_evaluation_id=risk_evaluation_id,
        symbol=symbol, side=side, qty=qty, stop_loss=stop_loss, take_profit=take_profit,
        mode=mode, status="PENDING",
    )
    session.add(o)
    session.flush()
    return o


def save_execution(session: Session, order_id: int, fill_qty: float, fill_price: float,
                    fee: float, is_partial: bool) -> Execution:
    ex = Execution(order_id=order_id, fill_qty=fill_qty, fill_price=fill_price,
                    fee=fee, is_partial=is_partial)
    session.add(ex)
    session.flush()
    return ex


def open_position(session: Session, symbol: str, side: str, qty: float,
                   avg_entry_price: float, stop_loss: float, take_profit: float | None) -> Position:
    p = Position(symbol=symbol, side=side, qty=qty, avg_entry_price=avg_entry_price,
                 stop_loss=stop_loss, take_profit=take_profit, status="OPEN")
    session.add(p)
    session.flush()
    return p


def close_position(session: Session, position: Position, realized_pnl: float, fees_paid: float) -> None:
    position.status = "CLOSED"
    position.realized_pnl = realized_pnl
    position.fees_paid = fees_paid
    position.closed_at = utcnow()
    session.flush()


def open_positions(session: Session, symbol: str | None = None) -> list[Position]:
    stmt = select(Position).where(Position.status == "OPEN")
    if symbol:
        stmt = stmt.where(Position.symbol == symbol)
    return list(session.execute(stmt).scalars().all())


def closed_positions(session: Session, symbol: str | None = None) -> list[Position]:
    stmt = select(Position).where(Position.status == "CLOSED").order_by(Position.closed_at)
    if symbol:
        stmt = stmt.where(Position.symbol == symbol)
    return list(session.execute(stmt).scalars().all())


def save_account_snapshot(session: Session, balance: float, equity: float,
                           unrealized_pnl: float, mode: str) -> AccountSnapshot:
    snap = AccountSnapshot(balance=balance, equity=equity, unrealized_pnl=unrealized_pnl, mode=mode)
    session.add(snap)
    session.flush()
    return snap


def latest_account_snapshot(session: Session) -> AccountSnapshot | None:
    stmt = select(AccountSnapshot).order_by(AccountSnapshot.taken_at.desc()).limit(1)
    return session.execute(stmt).scalar_one_or_none()


def recent_signals(session: Session, limit: int = 50) -> list[StrategySignal]:
    stmt = select(StrategySignal).order_by(StrategySignal.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def recent_ai_recommendations(session: Session, limit: int = 50) -> list[AIRecommendation]:
    stmt = select(AIRecommendation).order_by(AIRecommendation.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def recent_risk_evaluations(session: Session, limit: int = 50) -> list[RiskEvaluation]:
    stmt = select(RiskEvaluation).order_by(RiskEvaluation.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def recent_security_events(session: Session, limit: int = 50) -> list[SecurityEvent]:
    stmt = select(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def recent_failures(session: Session, limit: int = 50) -> list[FailureReconciliation]:
    stmt = select(FailureReconciliation).order_by(FailureReconciliation.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())
