"""Wires market data -> strategy -> risk -> execution (one tick), and
market data -> AI shadow agent (parallel, observation-only, never gates
execution). This is the only module allowed to call both the Risk Engine and
an Execution Engine, which keeps the "Risk Engine has sole authority" property
easy to audit: every path that reaches ExecutionEngine.submit() first went
through RiskEngine.evaluate() or RiskEngine.evaluate_close() in this file.

Correction v1.1: closing a position (opposing signal or a stop-loss/
take-profit touch) now goes through RiskEngine.evaluate_close() -- it no
longer fabricates an ApprovedOrder directly. See app/risk/engine.py.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from app.ai_shadow.agent import AIShadowAgent
from app.core.clock import RemoteTimeProvider, compute_clock_sync, utcnow
from app.core.config import Settings
from app.core.logging import get_logger, log_event
from app.execution.base import ExecutionEngine
from app.execution.idempotency import make_idempotency_key
from app.execution.reconciliation import reconcile_positions
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
        clock_provider: RemoteTimeProvider,
        price_state: dict[str, float] | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.market_data_provider = market_data_provider
        self.strategy_engine = strategy_engine
        self.risk_engine = risk_engine
        self.execution_engine = execution_engine
        self.ai_agent = ai_agent
        self.clock_provider = clock_provider
        # Shared with whatever price_provider fallback the execution engine
        # was built with; the orchestrator is the single writer, updated
        # from the candle that is actually driving each decision.
        self.price_state: dict[str, float] = price_state if price_state is not None else {}

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
            self.price_state[candle.symbol] = candle.close

            signal = self.strategy_engine.on_candle(candle)
            signal_row = repo.save_signal(
                session, signal.symbol, signal.direction, signal.justification,
                signal.observed_price, signal.atr, signal.params,
            )

            self._run_ai_shadow(session, signal, signal_row.id, candle.close)

            data_is_stale = self.market_data_provider.is_stale(
                self.settings.risk_max_data_staleness_seconds
            )

            clock_sync = compute_clock_sync(self.clock_provider, self.settings.risk_max_clock_drift_seconds)
            if not clock_sync.ok:
                state.trading_blocked = True
                state.block_reason = f"CLOCK_DRIFT: {clock_sync.error}"
                repo.record_security_event(session, "CLOCK_DRIFT_BLOCKED", clock_sync.error or "unknown")

            stop_take_result = self._check_stop_take(session, state, candle, data_is_stale, clock_sync)
            if stop_take_result is not None:
                return stop_take_result

            close_result = self._maybe_close_opposing_position(
                session, state, signal, signal_row.id, data_is_stale, clock_sync
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
                **self._common_risk_fields(state, data_is_stale, clock_sync),
            )

            risk_result = self.risk_engine.evaluate(signal, signal_row.id, context)
            risk_row = repo.save_risk_evaluation(
                session, signal_row.id, risk_result.approved, risk_result.reason, risk_result.checks
            )

            if not risk_result.approved or risk_result.approved_order is None:
                return {"status": "rejected", "reason": risk_result.reason}

            return self._submit_and_record(
                session, state, risk_row.id, risk_result.approved_order, candle.open_time, candle.close
            )

    def _common_risk_fields(self, state, data_is_stale: bool, clock_sync) -> dict:
        return dict(
            data_is_stale=data_is_stale,
            api_failure_count=state.api_failure_count,
            clock_drift_seconds=clock_sync.drift_seconds,
            kill_switch_engaged=state.kill_switch_engaged,
            trading_blocked=state.trading_blocked,
            state_ambiguous=state.state_ambiguous,
            cooldown_until=state.cooldown_until,
            now=utcnow(),
        )

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

    def _check_stop_take(self, session, state, candle, data_is_stale: bool, clock_sync) -> dict | None:
        """Evaluates stop-loss/take-profit for the open position (if any) on
        this candle's symbol, using the candle's high/low range. Runs before
        any new signal is considered, so an exit always takes priority over a
        fresh entry within the same tick.
        """
        positions = repo.open_positions(session, candle.symbol)
        if not positions:
            return None
        position = positions[0]

        if position.side == "BUY":
            stop_hit = position.stop_loss is not None and candle.low <= position.stop_loss
            target_hit = position.take_profit is not None and candle.high >= position.take_profit
        else:
            stop_hit = position.stop_loss is not None and candle.high >= position.stop_loss
            target_hit = position.take_profit is not None and candle.low <= position.take_profit

        if not stop_hit and not target_hit:
            return None

        # Conservative assumption (documented in docs/OPERACAO_DEMO.md): with
        # no intrabar sequencing available, if both stop and target were
        # touched within the same candle we assume the WORSE outcome (the
        # stop-loss) happened first.
        if stop_hit:
            trigger_price = position.stop_loss
            trigger_kind = "stop_loss"
            justification = (
                f"Stop-loss touched: candle range [{candle.low}, {candle.high}] crossed "
                f"stop {position.stop_loss}."
            )
            if target_hit:
                justification += (
                    " Take-profit was also touched in the same candle; conservative rule "
                    "assumes the stop-loss triggered first."
                )
        else:
            trigger_price = position.take_profit
            trigger_kind = "take_profit"
            justification = (
                f"Take-profit touched: candle range [{candle.low}, {candle.high}] crossed "
                f"target {position.take_profit}."
            )

        close_side = "SELL" if position.side == "BUY" else "BUY"
        signal_row = repo.save_signal(
            session, position.symbol, close_side, justification, candle.close, 0.0,
            {"trigger": trigger_kind, "trigger_price": trigger_price},
        )

        common_fields = self._common_risk_fields(state, data_is_stale, clock_sync)
        bucket = candle.open_time.strftime("%Y%m%dT%H%M") + f":{trigger_kind}"
        return self._close_position_via_risk(
            session, state, position, close_side, position.qty, signal_row.id,
            common_fields, trigger_price, bucket,
        )

    def _maybe_close_opposing_position(
        self, session, state, signal, signal_id: int, data_is_stale: bool, clock_sync
    ) -> dict | None:
        if signal.direction not in ("BUY", "SELL"):
            return None

        positions = repo.open_positions(session, signal.symbol)
        opposing = [p for p in positions if p.side != signal.direction]
        if not opposing:
            return None
        position = opposing[0]

        common_fields = self._common_risk_fields(state, data_is_stale, clock_sync)
        bucket = signal.created_at.strftime("%Y%m%dT%H%M") + ":close"
        return self._close_position_via_risk(
            session, state, position, signal.direction, position.qty, signal_id,
            common_fields, signal.observed_price, bucket,
        )

    def _close_position_via_risk(
        self, session, state, position, close_side: str, qty: float, signal_id: int,
        common_fields: dict, trigger_price: float, idempotency_bucket: str,
    ) -> dict:
        context = RiskContext(
            open_positions_count=0, open_exposure_usd=0.0, daily_realized_loss_usd=0.0,
            consecutive_losses=state.consecutive_losses, **common_fields,
        )
        risk_result = self.risk_engine.evaluate_close(
            signal_id=signal_id, symbol=position.symbol, close_side=close_side, qty=qty,
            position_exists=True, position_qty=position.qty, position_side=position.side,
            context=context,
        )
        risk_row = repo.save_risk_evaluation(
            session, signal_id, risk_result.approved, risk_result.reason, risk_result.checks
        )

        if not risk_result.approved or risk_result.approved_order is None:
            return {"status": "close_rejected", "reason": risk_result.reason}

        approved = risk_result.approved_order
        key = make_idempotency_key(approved, idempotency_bucket)
        existing = repo.find_order_by_idempotency_key(session, key)
        if existing:
            return {"status": "duplicate_suppressed", "order_id": existing.id}

        order_row = repo.save_order(
            session, key, risk_row.id, approved.symbol, approved.side, approved.qty,
            approved.stop_loss, approved.take_profit, mode=self.settings.mode.value, is_close=True,
        )

        fill = self.execution_engine.submit(approved, key, reference_price=trigger_price)
        order_row.status = fill.status
        order_row.exchange_order_id = fill.exchange_order_id

        if fill.status not in ("FILLED", "PARTIALLY_FILLED"):
            state.api_failure_count += 1
            repo.record_failure(session, "FAILURE", f"Close order {order_row.id} ended in status={fill.status}")
            self.reconcile(session, state)
            return {"status": "close_failed", "order_status": fill.status, "order_id": order_row.id}

        repo.save_execution(session, order_row.id, fill.fill_qty, fill.fill_price, fill.fee, fill.is_partial)

        direction = 1 if position.side == "BUY" else -1
        realized_pnl_delta = direction * (fill.fill_price - position.avg_entry_price) * fill.fill_qty

        closed_fully = fill.fill_qty >= position.qty - 1e-9
        if closed_fully:
            repo.close_position(session, position, realized_pnl_delta, fill.fee)
        else:
            repo.reduce_position(session, position, fill.fill_qty, realized_pnl_delta, fill.fee)

        if closed_fully:
            if realized_pnl_delta < 0:
                state.consecutive_losses += 1
            else:
                state.consecutive_losses = 0
            if state.consecutive_losses >= self.settings.risk_cooldown_after_losses:
                state.cooldown_until = utcnow() + timedelta(minutes=self.settings.risk_cooldown_minutes)
                repo.record_security_event(
                    session, "COOLDOWN_ENGAGED",
                    f"{state.consecutive_losses} consecutive losses; cooldown until {state.cooldown_until.isoformat()}.",
                )
                log_event(logger, 30, "cooldown_engaged", consecutive_losses=state.consecutive_losses)

        log_event(logger, 20, "position_closed" if closed_fully else "position_reduced",
                  symbol=position.symbol, realized_pnl=realized_pnl_delta, order_id=order_row.id)
        return {
            "status": "position_closed" if closed_fully else "position_reduced",
            "realized_pnl": realized_pnl_delta, "order_id": order_row.id,
        }

    def _submit_and_record(self, session, state, risk_evaluation_id: int, approved,
                            open_time, candle_close: float) -> dict:
        key = make_idempotency_key(approved, open_time.strftime("%Y%m%dT%H%M"))
        existing = repo.find_order_by_idempotency_key(session, key)
        if existing:
            return {"status": "duplicate_suppressed", "order_id": existing.id}

        order_row = repo.save_order(
            session, key, risk_evaluation_id, approved.symbol, approved.side, approved.qty,
            approved.stop_loss, approved.take_profit, mode=self.settings.mode.value, is_close=False,
        )

        fill = self.execution_engine.submit(approved, key, reference_price=candle_close)
        order_row.status = fill.status
        order_row.exchange_order_id = fill.exchange_order_id

        if fill.status in ("FILLED", "PARTIALLY_FILLED"):
            repo.save_execution(session, order_row.id, fill.fill_qty, fill.fill_price, fill.fee, fill.is_partial)
            repo.open_position(
                session, approved.symbol, approved.side, fill.fill_qty, fill.fill_price,
                approved.stop_loss, approved.take_profit, opening_fee=fill.fee,
            )
            return {"status": "order_filled", "order_id": order_row.id}

        state.api_failure_count += 1
        repo.record_failure(session, "FAILURE", f"Order {order_row.id} ended in status={fill.status}")
        self.reconcile(session, state)
        return {"status": "order_not_filled", "order_status": fill.status}

    def reconcile(self, session, state) -> None:
        """Compares locally persisted OPEN positions against what the
        execution engine reports for the exchange. Runs at orchestrator
        construction time (startup / after a restart), and again whenever an
        order submission ends in an unresolved/error status -- see callers
        above. Any mismatch, or failure to even reach the exchange, sets
        TRADING_BLOCKED and state_ambiguous=True; both the Risk Engine's
        common gates check state_ambiguous before approving anything.
        """
        local_positions = [
            {"symbol": p.symbol, "side": p.side, "qty": p.qty}
            for p in repo.open_positions(session)
        ]
        symbols = {p["symbol"] for p in local_positions}
        symbols.add(self.settings.symbol)

        remote_by_symbol: dict[str, dict | None] = {}
        try:
            for symbol in symbols:
                remote_by_symbol[symbol] = self.execution_engine.get_position(symbol)
        except Exception as exc:  # noqa: BLE001 - any failure to verify blocks trading
            state.state_ambiguous = True
            state.trading_blocked = True
            state.block_reason = f"RECONCILIATION_MISMATCH: could not query exchange positions ({exc})."
            repo.record_failure(session, "RECONCILIATION", state.block_reason)
            repo.record_security_event(session, "RECONCILIATION_FAILED", str(exc))
            return

        report = reconcile_positions(local_positions, remote_by_symbol)
        if report.ok:
            was_ambiguous = state.state_ambiguous
            state.state_ambiguous = False
            if was_ambiguous and state.block_reason and state.block_reason.startswith("RECONCILIATION_MISMATCH"):
                state.trading_blocked = False
                state.block_reason = None
            repo.record_failure(
                session, "RECONCILIATION", "Reconciliation OK: local and exchange positions match.",
                resolved=True,
            )
        else:
            state.state_ambiguous = True
            state.trading_blocked = True
            state.block_reason = "RECONCILIATION_MISMATCH: " + "; ".join(report.mismatches)
            repo.record_failure(session, "RECONCILIATION", state.block_reason)
            repo.record_security_event(session, "RECONCILIATION_MISMATCH", state.block_reason)
