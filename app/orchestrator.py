"""Wires market data -> strategy -> risk -> execution (one tick), and
market data -> AI shadow agent (parallel, observation-only, never gates
execution). This is the only module allowed to call both the Risk Engine and
an Execution Engine, which keeps the "Risk Engine has sole authority" property
easy to audit: every path that reaches ExecutionEngine.submit() first went
through RiskEngine.evaluate() or RiskEngine.evaluate_close() in this file.

Correction v1.1: closing a position (opposing signal or a stop-loss/
take-profit touch) now goes through RiskEngine.evaluate_close() -- it no
longer fabricates an ApprovedOrder directly. See app/risk/engine.py.

Correção v1.1 (Fase 2, réplica): `submit()` no longer blocks waiting for
confirmation -- see app/execution/base.py::SubmitAck/OrderStatusSnapshot.
The normal path here always calls `poll_order()` once immediately after a
successful submit (so PAPER_LOCAL/PAPER_LIVE and a fast-confirming
BYBIT_DEMO fake still resolve within the same tick, preserving every
existing regression test), and `_poll_open_orders()` re-polls anything
still non-terminal on a configurable periodic cadence -- this is what makes
a process restart with an order in flight, or a real exchange that takes
longer than one tick to fill, actually work instead of hanging or
re-submitting. Every fill, from any of these paths, is applied through the
single `app/execution/fill_service.py::apply_order_snapshot` -- there is no
second place that touches Execution/Position/session counters.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from app.ai_shadow.agent import AIShadowAgent
from app.core.clock import RemoteTimeProvider, compute_clock_sync, utcnow
from app.core.config import Settings
from app.core.logging import get_logger, log_event
from app.execution import fill_service
from app.execution.base import ExecutionEngine
from app.execution.funding import FUNDING_WINDOW_SECONDS, BybitFundingProvider, record_new_funding_events
from app.execution.idempotency import make_idempotency_key
from app.execution.order_state import OrderStatus, is_terminal
from app.execution.reconciliation import reconcile_orders, reconcile_positions
from app.market_data.base import CandleFetchStatus, MarketDataProvider
from app.persistence import repo
from app.persistence.models import OperationalSession
from app.risk.engine import RiskContext, RiskEngine
from app.sessions import increment as increment_session_counter
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
        funding_provider: "BybitFundingProvider | None" = None,
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
        self._last_open_order_poll_at: datetime | None = None
        # Correção v1.1 #6: only ever set for BYBIT_DEMO (the only mode
        # with private-endpoint credentials) -- None means funding stays
        # UNAVAILABLE rather than a fabricated/simulated value.
        self.funding_provider = funding_provider
        self._last_funding_poll_at: datetime | None = None
        # Correção operacional do poll loop v1.0: set from OUTSIDE (by
        # app/api/poll_engine.py's worker/supervisor) whenever the poll
        # engine itself is DEGRADADO/PARADO or its heartbeat has expired --
        # deliberately process-memory only (resets on restart, exactly like
        # the rest of the poll engine's health state). Read here so
        # RiskEngine.evaluate() can refuse new entries even in the
        # (should-be-impossible-post-fix, but defense-in-depth) case a tick
        # still runs while the engine is otherwise considered unhealthy.
        self.engine_degraded = False

    def tick(self) -> dict:
        from app.persistence.db import session_scope

        with session_scope(self.session_factory) as session:
            state = repo.get_or_create_system_state(session)

            # Fase 2, item 7.4: reconciliation now also runs periodically,
            # not only at startup or right after a failed order -- if it's
            # been longer than `reconciliation_interval_seconds` since the
            # last run, do one now, before anything else this tick. A
            # SystemState with no prior reconciliation at all
            # (last_reconciliation_at is None) is treated as "not yet due"
            # here rather than "infinitely stale" -- app/api/main.py always
            # runs one real reconciliation at startup before the first tick
            # in production; this only matters for tests that build an
            # Orchestrator directly without that startup call.
            # Correção de Datetimes v1.0: state.last_reconciliation_at (e todo
            # timestamp de domínio) já vem UTC-aware da camada ORM -- ver
            # app/persistence/temporal.py::UTCDateTime.
            last_reconciliation_at = state.last_reconciliation_at

            if last_reconciliation_at is not None:
                elapsed = (utcnow() - last_reconciliation_at).total_seconds()
                if elapsed >= self.settings.reconciliation_interval_seconds:
                    self.reconcile(session, state)
                    last_reconciliation_at = state.last_reconciliation_at

            if last_reconciliation_at is not None:
                delay = (utcnow() - last_reconciliation_at).total_seconds()
                state.reconciliation_stale = delay > self.settings.reconciliation_max_delay_seconds
            else:
                state.reconciliation_stale = False
            repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)

            # Correção v1.1 #1: real, persistent order-status polling -- an
            # order still non-terminal from a prior tick (or a prior
            # process, before a restart) is re-polled here on a configurable
            # cadence, never left hanging and never re-submitted.
            self._maybe_poll_open_orders(session, state)
            self._maybe_collect_funding(session, state)

            # Correction v1.4 #2: before polling, tell the provider the last
            # candle actually persisted for its symbol/timeframe -- cheap
            # (indexed) and makes a freshly constructed provider (e.g. right
            # after a process restart) resume backlog draining exactly where
            # it left off, never reprocessing or losing track.
            sync_cursor = getattr(self.market_data_provider, "sync_cursor", None)
            if sync_cursor is not None:
                provider_symbol = getattr(self.market_data_provider, "symbol", self.settings.symbol)
                provider_timeframe = getattr(self.market_data_provider, "timeframe", "1")
                persisted_open_time = repo.get_last_candle_open_time(session, provider_symbol, provider_timeframe)
                sync_cursor(persisted_open_time)

            fetch_result = self.market_data_provider.next_candle()

            if fetch_result.status == CandleFetchStatus.REPLAY_FINISHED:
                # The ONLY status allowed to end the orchestrator's loop.
                return {"status": "no_data"}

            if fetch_result.status == CandleFetchStatus.NO_NEW_CANDLE:
                return {"status": "no_new_candle"}

            if fetch_result.status == CandleFetchStatus.RETRYABLE_ERROR:
                state.api_failure_count += 1
                repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)
                detail = fetch_result.detail or "Falha temporária ao consultar dados de mercado."
                repo.record_failure(session, "FAILURE", detail)
                if state.trading_blocked:
                    repo.record_security_event(
                        session, "API_FAILURE_LIMIT_REACHED",
                        f"Bloqueio automático após {state.api_failure_count} falhas consecutivas de API.",
                    )
                return {"status": "retryable_error", "detail": detail}

            if fetch_result.status == CandleFetchStatus.FATAL_ERROR:
                state.api_failure_count += 1
                repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)
                detail = fetch_result.detail or "Falha grave e não recuperável ao consultar dados de mercado."
                repo.record_failure(session, "FAILURE", detail)
                repo.record_security_event(session, "FATAL_MARKET_DATA_ERROR", detail)
                return {"status": "fatal_error", "detail": detail}

            if fetch_result.status == CandleFetchStatus.GAP_DETECTED:
                # Correction v1.4 #2: an unrecoverable hole in the closed-
                # candle sequence is treated as an explicit, safe state --
                # never silently skipped over. Blocks trading like any other
                # data-integrity problem; does NOT end the polling loop
                # (only REPLAY_FINISHED does), so the operator can resolve
                # it and the process keeps observing without a restart.
                state.state_ambiguous = True
                repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)
                detail = fetch_result.detail or "Lacuna detectada na sequência de candles fechados."
                repo.record_failure(session, "FAILURE", detail)
                repo.record_security_event(session, "MARKET_DATA_GAP_DETECTED", detail)
                return {"status": "gap_detected", "detail": detail}

            candle = fetch_result.candle
            saved = repo.save_candle(
                session, candle.symbol, candle.timeframe, candle.open_time,
                candle.open, candle.high, candle.low, candle.close, candle.volume, candle.source,
            )
            if saved is None:
                # Correction v1.2 #2: a concurrent/duplicate write for the
                # same symbol+timeframe+open_time is a no-op, never a crash,
                # and never re-triggers strategy/AI/risk processing.
                return {"status": "duplicate_candle"}

            op_session = self._active_session(session, state)
            increment_session_counter(op_session, "candles_count")

            # A fresh candle was received and persisted: the API is healthy.
            state.api_failure_count = 0
            repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)

            self.price_state[candle.symbol] = candle.close

            signal = self.strategy_engine.on_candle(candle)
            signal_row = repo.save_signal(
                session, signal.symbol, signal.direction, signal.justification,
                signal.observed_price, signal.atr, signal.params,
            )
            increment_session_counter(op_session, "signals_count")

            self._run_ai_shadow(session, state, op_session, signal, signal_row.id, candle.close)

            data_is_stale = self.market_data_provider.is_stale(
                self.settings.risk_max_data_staleness_seconds
            )

            clock_sync = compute_clock_sync(self.clock_provider, self.settings.risk_max_clock_drift_seconds)
            was_out_of_sync = state.clock_out_of_sync
            state.clock_out_of_sync = not clock_sync.ok
            if not clock_sync.ok and not was_out_of_sync:
                repo.record_security_event(
                    session, "CLOCK_DRIFT_BLOCKED",
                    clock_sync.error or "Relógio local fora de sincronia; motivo desconhecido.",
                )
            repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)

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
            increment_session_counter(op_session, "approvals_count" if risk_result.approved else "rejections_count")

            if not risk_result.approved or risk_result.approved_order is None:
                return {"status": "rejected", "reason": risk_result.reason}

            return self._submit_and_record(
                session, state, risk_row.id, risk_result.approved_order, candle.open_time, candle.close
            )

    def _active_session(self, session, state) -> OperationalSession | None:
        """Fase 2, item 7.7: the OperationalSession counters are updated
        against, looked up cheaply by primary key. Returns None if no
        session is active yet (e.g. a test-built Orchestrator that never
        went through app.api.main.build_orchestrator) -- counters simply
        aren't incremented in that case (see app.sessions.increment)."""
        if state.active_session_id is None:
            return None
        return session.get(OperationalSession, state.active_session_id)

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
            reconciliation_stale=state.reconciliation_stale,
            operational_state=state.operational_state,
            engine_degraded=self.engine_degraded,
            protection_sync_pending=state.protection_sync_pending,
        )

    def _run_ai_shadow(self, session, state, op_session, signal, signal_id: int, price: float) -> None:
        """Fase 2, item 7.10: `market_context` gains session/position/risk
        context beyond the base strategy params -- strictly ADDITIVE plain
        data (no ORM objects, no execution/credential references), so the
        AI Shadow boundary (app/ai_shadow/guard.py -- no import of
        app.execution/pybit, no credential field names) stays intact."""
        if self.ai_agent is None:
            return

        position = None
        open_pos = repo.open_positions(session, signal.symbol)
        if open_pos:
            p = open_pos[0]
            position = {"side": p.side, "qty": p.qty, "avg_entry_price": p.avg_entry_price}

        market_context = {
            **signal.params,
            "price": price,
            "session": {
                "mode": self.settings.mode.value,
                "symbol": signal.symbol,
                "operational_state": state.operational_state,
                "session_uid": op_session.session_uid if op_session is not None else None,
            },
            "position": position,
            "risk": {
                "trading_blocked": state.trading_blocked,
                "consecutive_losses": state.consecutive_losses,
                "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
            },
            "metrics": {
                "open_positions_count": len(open_pos),
                "closed_positions_count": len(repo.closed_positions(session, signal.symbol)),
            },
        }
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
                "Saída da IA inválida ou indisponível.", [], result.provider_name,
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
                f"Stop-loss atingido: a faixa do candle [{candle.low}, {candle.high}] "
                f"cruzou o stop em {position.stop_loss}."
            )
            if target_hit:
                justification += (
                    " O take-profit também foi tocado no mesmo candle; a regra "
                    "conservadora assume que o stop-loss foi acionado primeiro."
                )
        else:
            trigger_price = position.take_profit
            trigger_kind = "take_profit"
            justification = (
                f"Take-profit atingido: a faixa do candle [{candle.low}, {candle.high}] "
                f"cruzou o alvo em {position.take_profit}."
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

    def _submit_and_track(self, session, state, order_row, approved, reference_price: float):
        """Correção v1.1 #1: the shared submit -> transition -> (immediate)
        poll -> apply-fill sequence used by both entry and close orders.
        Returns `(ack, snapshot_result)` -- `snapshot_result` is None when
        the exchange never even accepted the order (REJECTED/UNKNOWN)."""
        ack = self.execution_engine.submit(approved, order_row.idempotency_key, reference_price=reference_price)
        if ack.exchange_order_id:
            order_row.exchange_order_id = ack.exchange_order_id
        repo.transition_order_status(session, order_row, ack.status, detail=f"submit(): {ack.status.value}.")

        if ack.status != OrderStatus.SUBMITTED:
            return ack, None

        snapshot = self.execution_engine.poll_order(ack.exchange_order_id)
        result = fill_service.apply_order_snapshot(
            session, state, self._active_session(session, state), order_row, snapshot,
            is_close=order_row.is_close, max_api_failures=self.settings.risk_max_api_failures,
            execution_engine=self.execution_engine,
        )
        return ack, result

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
            reference_price=trigger_price,
        )
        increment_session_counter(self._active_session(session, state), "orders_count")

        ack, result = self._submit_and_track(session, state, order_row, approved, trigger_price)

        if result is None:
            state.api_failure_count += 1
            repo.record_failure(
                session, "FAILURE",
                f"Ordem de fechamento {order_row.id} não foi aceita pela corretora (status={ack.status.value}).",
            )
            increment_session_counter(self._active_session(session, state), "failures_count")
            self.reconcile(session, state)
            return {"status": "close_failed", "order_status": ack.status.value, "order_id": order_row.id}

        if result.new_fill_count == 0:
            if result.status == OrderStatus.UNKNOWN:
                state.api_failure_count += 1
                repo.record_failure(
                    session, "FAILURE",
                    f"Ordem de fechamento {order_row.id} terminou com status={result.status.value}.",
                )
                increment_session_counter(self._active_session(session, state), "failures_count")
                self.reconcile(session, state)
                return {"status": "close_failed", "order_status": result.status.value, "order_id": order_row.id}
            # Accepted, still no fill yet (real exchange not-yet-filled) --
            # the periodic poller (_poll_open_orders) will pick it up.
            return {"status": "close_pending", "order_id": order_row.id, "order_status": result.status.value}

        closed_fully = bool(result.closed_fully)
        if closed_fully:
            if result.realized_pnl_delta_total < 0:
                state.consecutive_losses += 1
            else:
                state.consecutive_losses = 0
            if state.consecutive_losses >= self.settings.risk_cooldown_after_losses:
                state.cooldown_until = utcnow() + timedelta(minutes=self.settings.risk_cooldown_minutes)
                repo.record_security_event(
                    session, "COOLDOWN_ENGAGED",
                    f"{state.consecutive_losses} perdas consecutivas; cooldown até "
                    f"{state.cooldown_until.isoformat()}.",
                )
                log_event(logger, 30, "cooldown_engaged", consecutive_losses=state.consecutive_losses)

        log_event(logger, 20, "position_closed" if closed_fully else "position_reduced",
                  symbol=position.symbol, realized_pnl=result.realized_pnl_delta_total, order_id=order_row.id)
        return {
            "status": "position_closed" if closed_fully else "position_reduced",
            "realized_pnl": result.realized_pnl_delta_total, "order_id": order_row.id,
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
            reference_price=candle_close,
        )
        increment_session_counter(self._active_session(session, state), "orders_count")

        ack, result = self._submit_and_track(session, state, order_row, approved, candle_close)

        if result is None:
            state.api_failure_count += 1
            repo.record_failure(
                session, "FAILURE",
                f"Ordem {order_row.id} não foi aceita pela corretora (status={ack.status.value}).",
            )
            increment_session_counter(self._active_session(session, state), "failures_count")
            self.reconcile(session, state)
            return {"status": "order_not_filled", "order_status": ack.status.value}

        if result.new_fill_count > 0:
            return {"status": "order_filled", "order_id": order_row.id}

        if result.status == OrderStatus.UNKNOWN:
            state.api_failure_count += 1
            repo.record_failure(session, "FAILURE", f"Ordem {order_row.id} terminou com status={result.status.value}.")
            increment_session_counter(self._active_session(session, state), "failures_count")
            self.reconcile(session, state)
            return {"status": "order_not_filled", "order_status": result.status.value}

        # Accepted, still no fill yet -- the periodic poller picks it up.
        return {"status": "order_pending", "order_id": order_row.id, "order_status": result.status.value}

    def _maybe_poll_open_orders(self, session, state) -> None:
        """Correção v1.1 #1: periodic, persistent order-status polling --
        gated by `open_order_poll_interval_seconds` so this doesn't hammer
        the exchange every tick. Every non-terminal order still tracked
        locally is re-polled and its fills (if any) applied through the
        exact same `fill_service.apply_order_snapshot` used by the
        immediate post-submit poll and the kill switch -- one code path,
        never a second one."""
        now = utcnow()
        last = self._last_open_order_poll_at
        if last is not None and (now - last).total_seconds() < self.settings.open_order_poll_interval_seconds:
            return
        self._last_open_order_poll_at = now

        op_session = self._active_session(session, state)
        for order in repo.non_terminal_orders(session, mode=self.settings.mode.value):
            if not order.exchange_order_id:
                continue  # never actually reached the exchange -- nothing to poll yet
            if is_terminal(OrderStatus(order.status)):
                continue
            snapshot = self.execution_engine.poll_order(order.exchange_order_id)
            fill_service.apply_order_snapshot(
                session, state, op_session, order, snapshot,
                is_close=order.is_close, max_api_failures=self.settings.risk_max_api_failures,
                execution_engine=self.execution_engine,
            )
            self._maybe_apply_partial_fill_policy(session, state, op_session, order, now)

    def _maybe_collect_funding(self, session, state) -> None:
        """Correção v1.1 #6 / v1.2 #3 / v1.3 #1: periodic, idempotent
        funding collection -- only when `funding_provider` was actually
        wired (BYBIT_DEMO; never PAPER_LIVE, which has no private
        credentials -- see app/api/main.py::build_orchestrator). Gated by
        `funding_poll_interval_seconds`, same periodic-gate pattern as
        `_maybe_poll_open_orders`.

        Correção v1.3 #1: `since` is read from the explicit, persisted
        `FundingCollectionCheckpoint` (`repo.get_funding_checkpoint`) --
        NEVER derived from the MAX `occurred_at` already recorded in
        `funding_events`. That approach was unsafe: a newest-first
        paginated response could persist a recent record from page 1 and
        then fail on an older page 2, and the next cycle's `since` would
        jump past the still-unfetched backlog, making it permanently
        unreachable. The checkpoint only ever advances
        (`repo.advance_funding_checkpoint`) once an ENTIRE window is
        proven complete -- never partially, never based on which records
        happened to be returned or in what order.

        The `[since, now]` gap is walked in fixed-size windows
        (`FUNDING_WINDOW_SECONDS`) -- each window's records are persisted
        as soon as they're gathered (never batched until the end, and
        never discarded even when the window itself turns out
        incomplete), and windows are walked in chronological order,
        stopping at the first incomplete one so a later, more-recent
        window is never collected (nor its checkpoint advanced) while an
        earlier gap is left unfilled. Any incomplete window is logged as a
        structured, unresolved failure -- the period is never presented as
        fully reconciled when it is not."""
        if self.funding_provider is None:
            return
        now = utcnow()
        last = self._last_funding_poll_at
        if last is not None and (now - last).total_seconds() < self.settings.funding_poll_interval_seconds:
            return
        self._last_funding_poll_at = now

        checkpoint = repo.get_funding_checkpoint(session, self.settings.symbol)
        window_start = (
            checkpoint.covered_until if checkpoint is not None
            else (now - timedelta(seconds=FUNDING_WINDOW_SECONDS))
        )

        try:
            while window_start < now:
                window_end = min(window_start + timedelta(seconds=FUNDING_WINDOW_SECONDS), now)
                records, complete = self.funding_provider.list_funding(
                    self.settings.symbol, since=window_start, until=window_end,
                )
                if records:
                    record_new_funding_events(session, records)
                if not complete:
                    detail = (
                        f"Coleta de funding incompleta para {self.settings.symbol} entre "
                        f"{window_start.isoformat()} e {window_end.isoformat()} -- será retomada no "
                        "próximo ciclo pela mesma janela, sem avançar o checkpoint de cobertura."
                    )
                    repo.record_failure(session, "FAILURE", detail)
                    repo.record_security_event(session, "FUNDING_COLLECTION_INCOMPLETE", detail)
                    break
                repo.advance_funding_checkpoint(session, self.settings.symbol, window_end)
                window_start = window_end
        except Exception as exc:  # noqa: BLE001 - a funding-collection failure never blocks trading
            detail = f"Não foi possível coletar funding da corretora: {exc}"
            repo.record_failure(session, "FAILURE", detail)
            repo.record_security_event(session, "FUNDING_COLLECTION_FAILED", detail)
            return

    def _maybe_apply_partial_fill_policy(self, session, state, op_session, order, now) -> None:
        """Correção v1.1 #2/#5: an order stuck PARTIALLY_FILLED longer than
        `partial_fill_timeout_seconds` is handled per `partial_fill_policy`
        -- WAIT (default) never times out; CANCEL_REMAINDER and
        EXPIRE_AND_CANCEL both request cancellation of the unfilled
        remainder, through the exact same CANCEL_PENDING -> request_cancel
        -> poll -> fill_service path used everywhere else (never a second,
        divergent cancellation code path)."""
        if OrderStatus(order.status) != OrderStatus.PARTIALLY_FILLED:
            return
        policy = self.settings.partial_fill_policy
        if policy == "WAIT":
            return
        stalled_for = (now - order.updated_at).total_seconds()
        if stalled_for <= self.settings.partial_fill_timeout_seconds:
            return

        repo.transition_order_status(
            session, order, OrderStatus.CANCEL_PENDING,
            detail=f"Prazo de fill parcial excedido ({policy}, {stalled_for:.0f}s).",
        )
        self.execution_engine.request_cancel(order.exchange_order_id)
        cancel_snapshot = self.execution_engine.poll_order(order.exchange_order_id)
        fill_service.apply_order_snapshot(
            session, state, op_session, order, cancel_snapshot,
            is_close=order.is_close, max_api_failures=self.settings.risk_max_api_failures,
            execution_engine=self.execution_engine,
        )

    def reconcile(self, session, state) -> None:
        """Compares locally persisted state against what the execution
        engine reports for the exchange -- both positions (Fase 1) and,
        since correção v1.1 #3, open orders (an order the exchange has that
        isn't tracked locally, or one tracked locally as non-terminal that
        the exchange no longer reports as open). Runs at orchestrator
        construction time (startup / after a restart), whenever an order
        submission ends in an unresolved/error status, and periodically
        (Fase 2, item 7.4 -- see the top of `tick()`).

        Position reconciliation and order reconciliation each run in their
        OWN try/except, each persisting their own structured result --
        correção v1.1 #3 item 7 explicitly requires that a failure in one
        half is never masked by the other half succeeding. The combined
        `reconciliation_diverged`/`state_ambiguous` is only cleared when
        BOTH halves reach the exchange and find no mismatch (a logical
        AND) -- a divergence found by either half, or a failure to even
        reach the exchange for either half, keeps the system blocked.
        `last_reconciliation_at` is always stamped once, whatever the
        outcome, so staleness tracking reflects the most recent attempt,
        not just the most recent success. Both structured results are
        persisted via `repo.record_failure(..., mismatches=...)`
        (correção v1.1 #3) so a result can be inspected programmatically,
        not just read as a paragraph.
        """
        op_session = self._active_session(session, state)
        increment_session_counter(op_session, "reconciliations_count")
        state.last_reconciliation_at = utcnow()

        position_ok = self._reconcile_positions_step(session, state, op_session)
        order_ok = self._reconcile_orders_step(session, state, op_session)
        self._retry_pending_protection_sync(session, state)

        # Fase 2, item 7.7/7.8: a reconciliation that actually completed at
        # least its position half (reached the exchange and compared,
        # whether or not it found a mismatch) satisfies the "reconciliação
        # inicial concluída" gate.
        if position_ok is not None:
            state.initialization_not_reconciled = False
        state.reconciliation_diverged = not (position_ok and order_ok)
        state.state_ambiguous = state.reconciliation_diverged
        repo.recompute_trading_blocked(state, self.settings.risk_max_api_failures)

    def _retry_pending_protection_sync(self, session, state) -> None:
        """Correção Stop/Take Pós-Preenchimento v1.1, Bloqueio 2: o retry
        idempotente para uma sincronização de proteção remota que falhou --
        chamado no boot (reconciliação inicial) e periodicamente (esta
        função roda a cada `reconcile()`), nunca dependendo de estado em
        memória para retomar após um reinício: consulta o banco (posições
        com `remote_protection_status != 'SYNCED'`) e tenta de novo, com os
        MESMOS níveis já persistidos na posição (nunca recalcula um valor
        diferente aqui -- só o fill que os definiu pode mudá-los)."""
        pending = [
            p for p in repo.open_positions(session) if p.remote_protection_status != "SYNCED"
        ]
        for position in pending:
            synced = self.execution_engine.sync_position_protection(
                position.symbol, position.side, position.stop_loss, position.take_profit,
            )
            position.remote_protection_status = "SYNCED" if synced else "PENDING"
            if synced:
                repo.record_security_event(
                    session, "POSITION_PROTECTION_SYNC_RECOVERED",
                    f"Sincronização da proteção remota confirmada para a posição {position.id} "
                    f"({position.symbol} {position.side}) após nova tentativa.",
                )
        if pending:
            session.flush()  # sessão autoflush=False -- sem isto, o recompute abaixo lê valores antigos
        repo.recompute_protection_sync_pending(session, state)

    def _reconcile_positions_step(self, session, state, op_session) -> bool | None:
        """Returns True (clean), False (diverged), or None (couldn't even
        reach the exchange to compare)."""
        local_positions = [
            {
                "symbol": p.symbol, "side": p.side, "qty": p.qty, "avg_entry_price": p.avg_entry_price,
                # Correção Stop/Take Pós-Preenchimento v1.1, Bloqueio 2:
                # comparado contra a proteção remota real quando disponível
                # (BYBIT_DEMO) -- ver reconcile_positions().
                "stop_loss": p.stop_loss, "take_profit": p.take_profit,
            }
            for p in repo.open_positions(session)
        ]
        symbols = {p["symbol"] for p in local_positions}
        symbols.add(self.settings.symbol)

        remote_by_symbol: dict[str, dict | None] = {}
        try:
            for symbol in symbols:
                remote_by_symbol[symbol] = self.execution_engine.get_position(symbol)
        except Exception as exc:  # noqa: BLE001 - any failure to verify blocks trading
            detail = f"Não foi possível consultar as posições na corretora para reconciliação: {exc}"
            repo.record_failure(session, "RECONCILIATION", detail, session_id=op_session.id if op_session else None)
            repo.record_security_event(session, "RECONCILIATION_FAILED", detail)
            return None

        report = reconcile_positions(local_positions, remote_by_symbol)
        if report.ok:
            repo.record_failure(
                session, "RECONCILIATION",
                "Reconciliação de posições OK: posições locais e da corretora coincidem.",
                resolved=True, mismatches=report.mismatches,
                session_id=op_session.id if op_session else None,
            )
            return True

        detail = "Divergência de reconciliação de posições: " + "; ".join(report.mismatches)
        repo.record_failure(
            session, "RECONCILIATION", detail, mismatches=report.mismatches,
            session_id=op_session.id if op_session else None,
        )
        repo.record_security_event(session, "RECONCILIATION_MISMATCH", detail)
        return False

    def _reconcile_orders_step(self, session, state, op_session) -> bool | None:
        """Correção v1.1 #3: (a) re-polls every locally non-terminal order
        to recover any fill missed by the periodic poller (through the same
        `fill_service.apply_order_snapshot` used everywhere else -- never a
        second path), then (b) compares the now-current set of locally
        non-terminal orders against `list_open_orders()` to detect an order
        the exchange has that isn't tracked locally at all. Returns True
        (clean), False (diverged), or None (couldn't reach the exchange)."""
        local_orders = repo.non_terminal_orders(session, mode=self.settings.mode.value)
        symbols = {o.symbol for o in local_orders}
        symbols.add(self.settings.symbol)
        try:
            for order in local_orders:
                if not order.exchange_order_id or is_terminal(OrderStatus(order.status)):
                    continue
                snapshot = self.execution_engine.poll_order(order.exchange_order_id)
                fill_service.apply_order_snapshot(
                    session, state, op_session, order, snapshot,
                    is_close=order.is_close, max_api_failures=self.settings.risk_max_api_failures,
                    execution_engine=self.execution_engine,
                )

            remote_open_orders: list[dict] = []
            for symbol in symbols:
                remote_open_orders.extend(self.execution_engine.list_open_orders(symbol))
        except Exception as exc:  # noqa: BLE001 - any failure to verify blocks trading
            detail = f"Não foi possível consultar as ordens abertas na corretora para reconciliação: {exc}"
            repo.record_failure(session, "RECONCILIATION", detail, session_id=op_session.id if op_session else None)
            repo.record_security_event(session, "RECONCILIATION_FAILED", detail)
            return None

        local_open_orders = [
            {"exchange_order_id": o.exchange_order_id, "side": o.side, "qty": o.qty}
            for o in repo.non_terminal_orders(session, mode=self.settings.mode.value)
            if o.exchange_order_id
        ]
        report = reconcile_orders(local_open_orders, remote_open_orders)
        if report.ok:
            repo.record_failure(
                session, "RECONCILIATION",
                "Reconciliação de ordens OK: ordens locais e da corretora coincidem.",
                resolved=True, mismatches=report.mismatches,
                session_id=op_session.id if op_session else None,
            )
            return True

        detail = "Divergência de reconciliação de ordens: " + "; ".join(report.mismatches)
        repo.record_failure(
            session, "RECONCILIATION", detail, mismatches=report.mismatches,
            session_id=op_session.id if op_session else None,
        )
        repo.record_security_event(session, "RECONCILIATION_MISMATCH", detail)
        return False
