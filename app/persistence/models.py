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
    # Free-text column for backward compatibility with the DB, but every
    # writer/reader goes through app.execution.order_state.OrderStatus and
    # app.persistence.repo.transition_order_status() (Fase 2, item 7.2) --
    # never written directly elsewhere.
    status: Mapped[str] = mapped_column(String(16), default="PENDING_SUBMIT")
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))  # PAPER_LOCAL | PAPER_LIVE | BYBIT_DEMO
    # Fase 2, item 7.2: cumulative fill bookkeeping across possibly multiple
    # fills of the same order -- SET semantics (not incremented), so
    # repeated confirmation polls of the same exchange order never double
    # count. See app.persistence.repo.record_fill().
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float] = mapped_column(Float, default=0.0)
    fees_total: Mapped[float] = mapped_column(Float, default=0.0)
    # Fase 2, item 7.6: the price the decision was made against (the
    # candle/trigger price passed as `reference_price` to
    # ExecutionEngine.submit()) -- needed to compute realized slippage
    # (avg_fill_price vs. this) per order. Nullable: a pre-Fase-2 order
    # migrated from an older schema never had this recorded.
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    risk_evaluation: Mapped["RiskEvaluation"] = relationship(back_populates="orders")
    executions: Mapped[list["Execution"]] = relationship(back_populates="order")


class OrderEvent(Base):
    """Fase 2, item 7.2: audit trail of every order status transition --
    written exclusively by app.persistence.repo.transition_order_status(),
    which validates the transition via app.execution.order_state first."""

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    # Fase 2, item 7.4/7.7: optional links so a reconciliation/failure entry
    # can be traced back to the specific order or operational session it
    # happened under -- nullable, since most historical rows (and most
    # reconciliation runs, which are symbol-wide, not order-specific) have
    # no single order to point at.
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("operational_sessions.id"), nullable=True)


class OperationalSession(Base):
    """Fase 2, item 7.7: one row per execution session -- created or resumed
    at process startup (app.sessions.start_or_resume_session), ended
    explicitly (app.sessions.end_session) on graceful shutdown or a fatal
    condition. Never mutated by anything outside app/sessions.py."""

    __tablename__ = "operational_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_uid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    mode: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    strategy_version: Mapped[str] = mapped_column(String(64))
    risk_config_json: Mapped[str] = mapped_column(Text)
    config_snapshot_json: Mapped[str] = mapped_column(Text)  # sanitized -- never contains secrets
    status: Mapped[str] = mapped_column(String(16), default="INICIALIZANDO")
    candles_count: Mapped[int] = mapped_column(Integer, default=0)
    signals_count: Mapped[int] = mapped_column(Integer, default=0)
    approvals_count: Mapped[int] = mapped_column(Integer, default=0)
    rejections_count: Mapped[int] = mapped_column(Integer, default=0)
    orders_count: Mapped[int] = mapped_column(Integer, default=0)
    fills_count: Mapped[int] = mapped_column(Integer, default=0)
    failures_count: Mapped[int] = mapped_column(Integer, default=0)
    reconciliations_count: Mapped[int] = mapped_column(Integer, default=0)


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
    # Fase 2, item 7.5: independent block-cause flags, each derived and
    # reset by its own code path -- never a shared/generic boolean.
    # recompute_trading_blocked() combines all of these (and the older ones
    # above) without ever letting clearing one silently clear another.
    reconciliation_diverged: Mapped[bool] = mapped_column(Boolean, default=False)
    reconciliation_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    order_state_unknown: Mapped[bool] = mapped_column(Boolean, default=False)
    initialization_not_reconciled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_reconciliation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Fase 2, item 7.8: process-is-running vs strategy-authorized-to-enter.
    # INICIALIZANDO -> OBSERVANDO -> ATIVO (operator action) -> PAUSADO
    # (operator action) -> BLOQUEADO (mirrors trading_blocked) -> ENCERRANDO.
    operational_state: Mapped[str] = mapped_column(String(16), default="INICIALIZANDO")
    active_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("operational_sessions.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
