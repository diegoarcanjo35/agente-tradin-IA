"""SQLAlchemy models. One table per persisted concern (spec section 4.6) so
every decision is traceable end-to-end: candle -> signal -> ai_recommendation
(parallel, non-blocking) -> risk_evaluation -> order -> execution -> position.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.clock import utcnow


class Base(DeclarativeBase):
    pass


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candle_symbol_timeframe_open_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="1m")
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))  # replay | bybit_demo
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StrategySignal(Base):
    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # BUY | SELL | HOLD
    justification: Mapped[str] = mapped_column(Text)
    observed_price: Mapped[float] = mapped_column(Float)
    atr: Mapped[float] = mapped_column(Float)
    params_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIRecommendation(Base):
    __tablename__ = "ai_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_signals.id"), nullable=True)
    recommendation: Mapped[str] = mapped_column(String(8))  # BUY | SELL | HOLD
    confidence: Mapped[float] = mapped_column(Float)
    reasoning_summary: Mapped[str] = mapped_column(String(500))
    risk_flags_json: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(64), default="unknown")
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskEvaluation(Base):
    __tablename__ = "risk_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("strategy_signals.id"))
    approved: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    checks_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    orders: Mapped[list["Order"]] = relationship(back_populates="risk_evaluation")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    risk_evaluation_id: Mapped[int] = mapped_column(ForeignKey("risk_evaluations.id"))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_close: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))  # PAPER_LOCAL | BYBIT_DEMO
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    risk_evaluation: Mapped["RiskEvaluation"] = relationship(back_populates="orders")
    executions: Mapped[list["Execution"]] = relationship(back_populates="order")


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    fill_qty: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    is_partial: Mapped[bool] = mapped_column(Boolean, default=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped["Order"] = relationship(back_populates="executions")


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    avg_entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN | CLOSED
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees_paid: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    balance: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(16))
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64))  # e.g. PRODUCTION_ENDPOINT_BLOCKED, KILL_SWITCH
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FailureReconciliation(Base):
    __tablename__ = "failures_reconciliations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))  # FAILURE | RECONCILIATION
    detail: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemState(Base):
    """Singleton-ish row (id=1) holding the live TRADING_BLOCKED / kill-switch state."""

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    trading_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kill_switch_engaged: Mapped[bool] = mapped_column(Boolean, default=False)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    api_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    state_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    clock_out_of_sync: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
