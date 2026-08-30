"""Wires market data -> strategy -> risk -> execution (one tick), and
market data -> AI shadow agent (parallel, observation-only, never gates
execution). This is the only module allowed to call both the Risk Engine and
an Execution Engine, which keeps the "Risk Engine has sole authority" property
easy to audit: grep for ExecutionEngine.submit(...) call sites.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from app.ai_shadow.agent import AIShadowAgent
from app.core.clock import utcnow
from app.core.config import RunMode, Settings
from app.core.logging import get_logger, log_event
from app.execution.base import ExecutionEngine
from app.execution.idempotency import make_idempotency_key
from app.market_data.base import MarketDataProvider
from app.persistence import repo
from app.risk.engine import RiskContext, RiskEngine
from app.strategy.engine import StrategyEngine

logger = get_logger(__name__)


def _today_start_utc(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        session_factory,
        market_data_provider: MarketDataProvider,
        strategy_engine: StrategyEngine,
        risk_engine: RiskEngine,
        execution_engine: ExecutionEngine,
        ai_agent: AIShadowAgent | None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.market_data_provider = market_data_provider
        self.strategy_engine = strategy_engine
        self.risk_engine = risk_engine
        self.execution_engine = execution_engine
        self.ai_agent = ai_agent

    def tick(self) -> dict:
        from app.persistence.db import session_scope

        with session_scope(self.session_factory) as session:
            state = repo.get_or_create_system_state(session)

            candle = self.market_data_provider.next_candle()
            if candle is None:
                return {"status": "no_data"}

            repo.save_candle(
                session, candle.symbol, candle.timeframe, candle.open_time,
                candle.open, candle.high, candle.low, candle.close, candle.volume, candle.source,
            )

            signal = self.strategy_engine.on_candle(candle)
            signal_row = repo.save_signal(
                session, signal.symbol, signal.direction, signal.justification,
                signal.observed_price, signal.atr, signal.params,
            )

            self._run_ai_shadow(session, signal, signal_row.id, candle.close)

            data_is_stale = self.market_data_provider.is_stale(
                self.settings.risk_max_data_staleness_seconds
            )

            close_result = self._maybe_close_opposing_position(
                session, state, signal, data_is_stale
            )
            if close_result is not None:
                return close_result

            if signal.direction == "HOLD":
                return {"status": "hold"}

            open_pos = repo.open_positions(session, signal.symbol)
            all_open = repo.open_positions(session)
            open_exposure = sum(p.qty * p.avg_entry_price for p in all_open)
            today_start = _today_start_utc(utcnow())
            daily_loss = sum(
                -p.realized_pnl
                for p in repo.closed_positions(session)
                if p.realized_pnl < 0 and p.closed_at and p.closed_at >= today_start
            )

            context = RiskContext(
                open_positions_count=len(open_pos),
                open_exposure_usd=open_exposure,
                daily_realized_loss_usd=daily_loss,
                consecutive_losses=state.consecutive_losses,
                data_is_stale=data_is_stale,
                api_failure_count=state.api_failure_count,
                clock_drift_seconds=0.0,
                kill_switch_engaged=state.kill_switch_engaged,
                trading_blocked=state.trading_blocked,
                cooldown_until=state.cooldown_until,
                now=utcnow(),
            )

            risk_result = self.risk_engine.evaluate(signal, signal_row.id, context)
            risk_row = repo.save_risk_evaluation(
                session, signal_row.id, risk_result.approved, risk_result.reason, risk_result.checks
            )

            if not risk_result.approved or risk_result.approved_order is None:
                return {"status": "rejected", "reason": risk_result.reason}

            return self._submit_and_record(session, state, risk_row.id, risk_result.approved_order,
                                            candle.open_time)

    def _run_ai_shadow(self, session, signal, signal_id: int, price: float) -> None:
        if self.ai_agent is None:
            return
        market_context = {**signal.params, "price": price}
        result = self.ai_agent.observe(signal.symbol, market_context)
        if result is None:
            return
        if result.is_valid and result.output is not None:
            repo.save_ai_recommendation(
                session, signal.symbol, signal_id, result.output.recommendation,
                result.output.confidence, result.output.reasoning_summary,
                result.output.risk_flags, result.provider_name, result.model_version,
                True, None,
            )
        else:
            repo.save_ai_recommendation(
                session, signal.symbol, signal_id, "HOLD", 0.0,
                "invalid or unavailable AI output", [], result.provider_name,
                result.model_version, False, result.rejection_reason,
            )

    def _maybe_close_opposing_position(self, session, state, signal, data_is_stale: bool) -> dict | None:
        """Closing an existing position is treated separately from opening a
        new one: it reduces risk rather than adding it, so it is permitted
        even while limits that gate *new* exposure (daily loss, concurrent
        position count, exposure headroom) would otherwise block. Kill switch,
        TRADING_BLOCKED, and stale data still apply -- we never act on bad
        data or while explicitly halted.
        """
        if signal.direction not in ("BUY", "SELL"):
            return None
        if state.kill_switch_engaged or state.trading_blocked or data_is_stale:
            return None

        positions = repo.open_positions(session, signal.symbol)
        opposing = [p for p in positions if p.side != signal.direction]
        if not opposing:
            return None
        position = opposing[0]

        close_side = "SELL" if position.side == "BUY" else "BUY"
        from app.risk.engine import ApprovedOrder, _RiskApprovalToken

        approved = ApprovedOrder(
            signal_id=0, symbol=signal.symbol, side=close_side, qty=position.qty,
            stop_loss=signal.observed_price, take_profit=None, token=_RiskApprovalToken(),
        )
        key = make_idempotency_key(approved, signal.created_at.strftime("%Y%m%dT%H%M") + ":close")
        existing = repo.find_order_by_idempotency_key(session, key)
        if existing:
            return {"status": "duplicate_suppressed", "order_id": existing.id}

        fill = self.execution_engine.submit(approved, key)
        if fill.status not in ("FILLED", "PARTIALLY_FILLED"):
            repo.record_failure(session, "FAILURE", f"Close order failed: status={fill.status}")
            return {"status": "close_failed"}

        direction = 1 if position.side == "BUY" else -1
        realized_pnl = direction * (fill.fill_price - position.avg_entry_price) * fill.fill_qty
        repo.close_position(session, position, realized_pnl, fill.fee)

        if realized_pnl < 0:
            state.consecutive_losses += 1
        else:
            state.consecutive_losses = 0

        from app.core.clock import utcnow as _utcnow
        from datetime import timedelta

        if state.consecutive_losses >= self.settings.risk_cooldown_after_losses:
            state.cooldown_until = _utcnow() + timedelta(minutes=self.settings.risk_cooldown_minutes)
            log_event(logger, 30, "cooldown_engaged", consecutive_losses=state.consecutive_losses)

        log_event(logger, 20, "position_closed", symbol=signal.symbol, realized_pnl=realized_pnl)
        return {"status": "position_closed", "realized_pnl": realized_pnl}

    def _submit_and_record(self, session, state, risk_evaluation_id: int, approved,
                            open_time) -> dict:
        key = make_idempotency_key(approved, open_time.strftime("%Y%m%dT%H%M"))
        existing = repo.find_order_by_idempotency_key(session, key)
        if existing:
            return {"status": "duplicate_suppressed", "order_id": existing.id}

        order_row = repo.save_order(
            session, key, risk_evaluation_id, approved.symbol, approved.side, approved.qty,
            approved.stop_loss, approved.take_profit, mode=self.settings.mode.value,
        )

        fill = self.execution_engine.submit(approved, key)
        order_row.status = fill.status
        order_row.exchange_order_id = fill.exchange_order_id

        if fill.status in ("FILLED", "PARTIALLY_FILLED"):
            repo.save_execution(session, order_row.id, fill.fill_qty, fill.fill_price, fill.fee, fill.is_partial)
            repo.open_position(
                session, approved.symbol, approved.side, fill.fill_qty, fill.fill_price,
                approved.stop_loss, approved.take_profit,
            )
            return {"status": "order_filled", "order_id": order_row.id}

        state.api_failure_count += 1
        repo.record_failure(session, "FAILURE", f"Order {order_row.id} ended in status={fill.status}")
        return {"status": "order_not_filled", "order_status": fill.status}
