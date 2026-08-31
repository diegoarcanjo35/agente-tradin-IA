"""Correção Operacional do Poll Loop v1.0.

Reproduced incident: the old `_poll_loop` in `app/api/main.py` was exactly

    while True:
        result = orch.tick()
        ...
        await asyncio.sleep(interval)

with NO exception barrier around `orch.tick()`. Any unexpected exception
propagated out of the `while` loop, silently ending the `asyncio.Task` the
lifespan had created with `asyncio.create_task()`. Nothing else observed
this: the FastAPI/uvicorn HTTP server is a completely separate coroutine
(the ASGI request-handling loop), so `/api/state` and the whole dashboard
kept responding normally, showing a system that "looked" healthy purely
because the web server was up -- while the market engine had actually
stopped observing candles, generating signals, or evaluating risk
entirely, with zero log line, zero security event, zero visible signal
anywhere. See `tests/test_poll_loop_supervision.py` for a direct
reproduction of this exact defect.

This module replaces that single unprotected loop with two cooperating
pieces:

  - `poll_worker`: the actual tick loop. Every `orch.tick()` call runs
    isolated from the FastAPI event loop (a dedicated, bounded, single-
    worker thread executor + an explicit `asyncio.wait_for` timeout -- a
    slow market call can never freeze the panel/API), wrapped in its own
    per-iteration exception barrier. An unexpected exception is caught,
    logged with a full traceback, recorded as a structured failure +
    security event (in a FRESH DB session/transaction, since the tick's
    own transaction may have rolled back), and marks the engine
    DEGRADADO with a bounded exponential backoff before the next
    attempt -- it is NEVER allowed to kill this coroutine.
    `asyncio.CancelledError` is deliberately never treated as a failure
    (always re-raised immediately) so graceful shutdown keeps working.

  - `supervise_poll_loop`: the outer supervisor. If `poll_worker` itself
    terminates unexpectedly anyway (a bug inside its own barrier, or
    something external killing it), the supervisor detects this via
    `task.exception()` (never leaving it to asyncio's own delayed "Task
    exception was never retrieved" warning), logs it immediately, marks
    the engine PARADO, and restarts a single new worker task after a
    bounded backoff -- by construction, exactly one worker task exists at
    any time (the supervisor always fully awaits the previous one being
    `done()` before creating a new one). The supervisor also independently
    monitors the heartbeat (a worker that is technically still running
    but whose last successful tick is older than
    `poll_heartbeat_max_age_seconds` is caught here too, logging the
    incident exactly once per episode -- never a log-storm from repeated
    checks).
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.core.clock import utcnow
from app.core.logging import get_logger, log_event
from app.persistence import repo
from app.persistence.db import session_scope

logger = get_logger(__name__)


class PollEngineStatus(str, Enum):
    INICIANDO = "INICIANDO"
    SAUDAVEL = "SAUDAVEL"
    DEGRADADO = "DEGRADADO"
    PARADO = "PARADO"
    ENCERRANDO = "ENCERRANDO"


@dataclass
class PollHealth:
    """Correção operacional do poll loop v1.0, item 4/6: process-memory
    only, deliberately never persisted to the database -- it resets on
    every restart, which is correct (a fresh process starts with a fresh
    heartbeat). What IS persisted, for historical audit, are the
    INCIDENTS themselves: every unexpected-exception tick and every dead-
    task/heartbeat-expired episode is recorded via
    `repo.record_failure`/`repo.record_security_event` (the existing
    `failures_reconciliations`/`security_events` tables -- no new
    migration needed for that)."""

    status: PollEngineStatus = PollEngineStatus.INICIANDO
    poll_last_started_at: datetime | None = None
    poll_last_completed_at: datetime | None = None
    poll_last_success_at: datetime | None = None
    poll_consecutive_failures: int = 0
    poll_last_error: str | None = None
    restart_count: int = 0
    # Correção item 4: "registrar falha/evento uma única vez por
    # incidente, sem tempestade de logs" -- tracks whether the CURRENT
    # heartbeat-expired episode has already been recorded, so a check
    # running every few seconds (or a dashboard polling /api/state every
    # 2s) never re-logs the same ongoing incident.
    heartbeat_alert_active: bool = False
    # Correção v1.1 #3: same one-alert-per-episode guard, for a SINGLE
    # tick that keeps exceeding `poll_tick_timeout_seconds` while still
    # genuinely running (never abandoned -- see `poll_worker`) -- without
    # this, re-awaiting the same stuck future would log/record a fresh
    # incident on every single re-check.
    timeout_alert_active: bool = False

    def as_dict(self) -> dict:
        return {
            "poll_loop_status": self.status.value,
            "poll_last_started_at": self.poll_last_started_at.isoformat() if self.poll_last_started_at else None,
            "poll_last_completed_at": (
                self.poll_last_completed_at.isoformat() if self.poll_last_completed_at else None
            ),
            "poll_last_success_at": self.poll_last_success_at.isoformat() if self.poll_last_success_at else None,
            "poll_consecutive_failures": self.poll_consecutive_failures,
            "poll_last_error": self.poll_last_error,
            "poll_restart_count": self.restart_count,
        }


def _sanitize_error(exc: BaseException) -> str:
    """Bounded, credential-free summary -- never the full exception repr
    (some library exceptions embed request URLs/payloads); just the
    exception type and a truncated message."""
    message = str(exc)
    if len(message) > 300:
        message = message[:300] + "…"
    return f"{type(exc).__name__}: {message}"


def is_heartbeat_expired(health: PollHealth, max_age_seconds: float, now: datetime | None = None) -> bool:
    """True once the engine has been running long enough to have had a
    chance to succeed at least once (or fail) and that reference point is
    older than `max_age_seconds`. A brand-new engine that hasn't even
    attempted its first tick yet is NOT considered heartbeat-expired --
    that's `INICIANDO`, a different, non-alarming state."""
    now = now or utcnow()
    reference = health.poll_last_success_at or health.poll_last_started_at
    if reference is None:
        return False
    return (now - reference).total_seconds() > max_age_seconds


def engine_unhealthy(health: PollHealth, max_heartbeat_age_seconds: float) -> bool:
    """Single source of truth for "may new entries be authorized" -- used
    by both `POST /api/operational-state/activate` and
    `RiskEngine.evaluate()` (via `Orchestrator.engine_degraded`).

    Correção v1.1 #2: `INICIANDO` and `ENCERRANDO` are NEVER eligible for
    new entries either -- the audited gap was exactly that `poll_worker`
    used to mark `SAUDAVEL` before completing even a single tick, leaving
    a real window where an operator could activate entries with zero
    proof the engine could actually reach/process the market. Only
    `SAUDAVEL` (which now requires at least one genuinely successful tick
    -- see `poll_worker`) is ever eligible, on top of a non-expired
    heartbeat."""
    if health.status != PollEngineStatus.SAUDAVEL:
        return True
    return is_heartbeat_expired(health, max_heartbeat_age_seconds)


def _record_incident(orch, event_type: str, detail_pt: str, sanitized_error: str | None) -> None:
    """Correção item 2/3/4: persists the incident in a FRESH transaction --
    never reuses/depends on whatever transaction the failed tick left
    behind (which may have rolled back), so the incident itself is never
    lost even when the tick that caused it lost all its own work.
    `event_type` distinguishes the three incident classes this module can
    raise (`POLL_LOOP_TICK_FAILED` / `POLL_LOOP_TASK_DIED` /
    `POLL_LOOP_HEARTBEAT_EXPIRED`) so the audit trail never conflates
    them."""
    full_detail = f"{detail_pt} Detalhe: {sanitized_error}" if sanitized_error else detail_pt
    try:
        with session_scope(orch.session_factory) as session:
            repo.record_failure(session, "FAILURE", full_detail)
            repo.record_security_event(session, event_type, full_detail)
    except Exception:  # noqa: BLE001 - recording the incident must never itself crash anything
        logger.exception("failed_to_record_poll_incident")


def _record_recovery(orch) -> None:
    try:
        with session_scope(orch.session_factory) as session:
            repo.record_failure(
                session, "FAILURE",
                "Motor de mercado recuperado após falha -- ciclos consecutivos saudáveis confirmados.",
                resolved=True,
            )
            repo.record_security_event(
                session, "POLL_LOOP_RECOVERED", "Motor de mercado voltou a operar normalmente."
            )
    except Exception:  # noqa: BLE001
        logger.exception("failed_to_record_poll_recovery")


def _maybe_recover(app, health: PollHealth, orch, consecutive_healthy: int, settings) -> None:
    """Correção v1.1 #1/#2: a SINGLE unified recovery rule, replacing the
    old `if health.status == DEGRADADO: ...` check that could never fire
    once the supervisor's independent heartbeat watch had already moved
    the status to `PARADO` (correção v1.1 #1's exact reproduction: a tick
    fails, the heartbeat expires DURING the backoff, the supervisor sets
    `PARADO` -- and the old code, checking only `== DEGRADADO`, then
    stayed `PARADO` forever even after dozens of later successful ticks).

    `INICIANDO` needs only ONE genuinely successful tick to become
    `SAUDAVEL` (correção v1.1 #2 -- proof the engine can actually reach/
    process the market at all) but is never treated as a "recovery" (it
    was never broken, so no `POLL_LOOP_RECOVERED` event is recorded).
    `DEGRADADO`/`PARADO` require `poll_healthy_ticks_to_recover`
    consecutive successes, exactly as before, and DO record one recovery
    event -- never more than one per episode, since this only fires on the
    transition itself."""
    if health.status == PollEngineStatus.SAUDAVEL:
        return
    was_broken = health.status in (PollEngineStatus.DEGRADADO, PollEngineStatus.PARADO)
    required = 1 if health.status == PollEngineStatus.INICIANDO else settings.poll_healthy_ticks_to_recover
    if consecutive_healthy < required:
        return
    health.status = PollEngineStatus.SAUDAVEL
    orch.engine_degraded = False
    # Correção v1.1 (validação complementar): never leave a stale error
    # message behind once genuinely recovered -- /api/state and the panel
    # must never show SAUDAVEL alongside a misleading old error.
    health.poll_last_error = None
    health.heartbeat_alert_active = False
    health.timeout_alert_active = False
    if was_broken:
        _record_recovery(orch)


async def poll_worker(app) -> None:
    """Correção item 2 / v1.1 #3: the tick loop, now with a per-iteration
    exception barrier, isolated/timed-out execution, and bounded backoff +
    recovery tracking. Never returns except when
    `CandleFetchStatus.REPLAY_FINISHED` is reached (surfaced as
    `status == "no_data"`) or when cancelled.

    Correção v1.1 #3: a Python thread cannot be forcibly killed, so a tick
    that exceeds `poll_tick_timeout_seconds` is NEVER abandoned -- the
    SAME underlying future (protected by `asyncio.shield`, so re-awaiting
    it never re-submits a new job) is awaited again on every subsequent
    loop iteration until it genuinely resolves, one way or another. This
    guarantees, structurally: at most one `orch.tick()` in flight ever;
    no growing queue of cancelled attempts; no new attempt submitted while
    the previous one is still alive. `app.state.poll_in_flight_future`
    exposes the live future so `app.api.main._graceful_shutdown` can wait
    for it to genuinely finish BEFORE running the final reconciliation/
    `end_session()` -- never let a late tick mutate the database after
    those have already run."""
    from app.core.config import RunMode  # local import: avoids a cycle at module load time

    orch = app.state.orchestrator
    settings = app.state.settings
    health: PollHealth = app.state.poll_health

    interval = (
        settings.bybit_poll_interval_seconds
        if settings.mode in (RunMode.BYBIT_DEMO, RunMode.PAPER_LIVE)
        else settings.replay_poll_interval_seconds
    )
    backoff = settings.poll_backoff_initial_seconds
    consecutive_healthy = 0
    loop = asyncio.get_running_loop()

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="poll-tick")
    pending_future: asyncio.Future | None = None
    try:
        while True:
            if pending_future is None:
                health.poll_last_started_at = utcnow()
                pending_future = loop.run_in_executor(executor, orch.tick)
                app.state.poll_in_flight_future = pending_future

            try:
                result = await asyncio.wait_for(
                    asyncio.shield(pending_future), timeout=settings.poll_tick_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # Correção v1.1 #3: the future is SHIELDED -- it was never
                # cancelled and keeps running for real in its thread.
                # `pending_future` is deliberately NOT reset here, so the
                # next loop iteration re-awaits this exact same future
                # instead of submitting a second one.
                consecutive_healthy = 0
                health.poll_consecutive_failures += 1
                health.status = PollEngineStatus.DEGRADADO
                orch.engine_degraded = True
                health.poll_last_error = (
                    f"TimeoutError: tick em execução há mais de {settings.poll_tick_timeout_seconds}s "
                    "(ainda em andamento, não abandonado)"
                )
                if not health.timeout_alert_active:
                    # Correção v1.1 (sem tempestade de logs): só o PRIMEIRO
                    # timeout deste mesmo tick pendurado gera log/incidente
                    # -- re-checagens seguintes do MESMO tick não duplicam.
                    health.timeout_alert_active = True
                    logger.error("poll_tick_timeout_still_running")
                    _record_incident(
                        orch, "POLL_LOOP_TICK_FAILED",
                        "Ciclo de mercado excedeu o tempo limite configurado (ainda em execução, "
                        "aguardando conclusão real antes de qualquer nova tentativa).",
                        health.poll_last_error,
                    )
                await asyncio.sleep(min(backoff, settings.poll_backoff_max_seconds))
                backoff = min(backoff * 2, settings.poll_backoff_max_seconds)
                continue
            except Exception as exc:  # noqa: BLE001 - this barrier is the entire point of this module
                # The future genuinely finished (not a timeout) with an
                # unexpected exception -- free it up for a fresh attempt.
                pending_future = None
                app.state.poll_in_flight_future = None
                consecutive_healthy = 0
                health.poll_last_completed_at = utcnow()
                health.poll_consecutive_failures += 1
                sanitized = _sanitize_error(exc)
                health.poll_last_error = sanitized
                health.status = PollEngineStatus.DEGRADADO
                orch.engine_degraded = True
                health.timeout_alert_active = False
                logger.exception("poll_tick_unexpected_error")
                _record_incident(
                    orch, "POLL_LOOP_TICK_FAILED",
                    "Falha inesperada no ciclo de mercado (motor de mercado).", sanitized,
                )
                await asyncio.sleep(min(backoff, settings.poll_backoff_max_seconds))
                backoff = min(backoff * 2, settings.poll_backoff_max_seconds)
                continue

            # The future genuinely finished successfully.
            pending_future = None
            app.state.poll_in_flight_future = None
            health.timeout_alert_active = False
            health.poll_last_completed_at = utcnow()
            health.poll_last_success_at = utcnow()
            health.poll_consecutive_failures = 0
            backoff = settings.poll_backoff_initial_seconds
            consecutive_healthy += 1
            _maybe_recover(app, health, orch, consecutive_healthy, settings)

            status = result.get("status")
            if status == "no_data":
                app.state.replay_done = True
                health.status = PollEngineStatus.PARADO
                log_event(logger, 20, "replay_complete")
                return
            if status == "retryable_error":
                log_event(logger, 30, "market_data_retryable_error", detail=result.get("detail"))
            elif status == "fatal_error":
                log_event(logger, 40, "market_data_fatal_error", detail=result.get("detail"))

            await asyncio.sleep(interval)
    finally:
        executor.shutdown(wait=False, cancel_futures=False)


async def wait_for_in_flight_tick_before_shutdown(app) -> None:
    """Correção v1.1 #3: called by `app/api/main.py::_graceful_shutdown`
    BEFORE running the final reconciliation / `end_session()` -- an
    absolute guarantee, not best-effort, that no tick can still mutate
    the database after those run. If a tick is genuinely still in flight
    (the future is not done), this waits for it for real, with NO
    artificial timeout that would let shutdown proceed regardless: it
    only logs a single warning (explicit, testable) if the wait exceeds
    `poll_tick_timeout_seconds`, and then keeps waiting for the actual
    conclusion -- never abandons it, never lets shutdown race ahead of a
    live database write."""
    orch = app.state.orchestrator
    settings = app.state.settings
    in_flight = getattr(app.state, "poll_in_flight_future", None)
    if in_flight is None or in_flight.done():
        return

    logger.warning("poll_loop_shutdown_waiting_for_in_flight_tick")
    try:
        await asyncio.wait_for(asyncio.shield(in_flight), timeout=settings.poll_tick_timeout_seconds)
        return
    except asyncio.TimeoutError:
        pass
    except Exception:  # noqa: BLE001 - the tick's own outcome is irrelevant here, only its completion matters
        return

    detail = (
        f"Tick em voo não terminou em {settings.poll_tick_timeout_seconds}s durante o encerramento -- "
        "aguardando a conclusão real antes de prosseguir com a reconciliação final/encerramento de sessão."
    )
    _record_incident(orch, "POLL_LOOP_SHUTDOWN_TICK_PENDING", detail, None)
    try:
        await asyncio.shield(in_flight)  # sem limite -- garantia absoluta, nunca abandona
    except Exception:  # noqa: BLE001 - only completion matters here, not the tick's own outcome
        pass


async def supervise_poll_loop(app) -> None:
    """Correção item 3/4: supervises `poll_worker` -- restarts it (with
    bounded backoff, never two workers at once) if it ever terminates
    unexpectedly, and independently watches the heartbeat so a "worker
    technically alive but stuck somewhere the internal barrier can't see"
    scenario is still caught."""
    settings = app.state.settings
    health: PollHealth = app.state.poll_health
    orch = app.state.orchestrator
    backoff = settings.poll_backoff_initial_seconds
    # Correção item 3/4: floor pequeno (não 1s+) para que a checagem de
    # heartbeat continue proporcional mesmo quando
    # poll_heartbeat_max_age_seconds é configurado bem baixo (testes) --
    # em produção (default 60s) isso ainda dá um intervalo de 15s.
    heartbeat_check_interval = max(0.05, settings.poll_heartbeat_max_age_seconds / 4)

    worker_task: asyncio.Task | None = None
    try:
        while True:
            worker_task = asyncio.create_task(poll_worker(app), name="poll-worker")
            app.state.poll_worker_task = worker_task
            # Correção item 4: anchors the heartbeat reference point the
            # moment a worker (re)starts, even before it gets around to
            # its own first `poll_last_started_at` update -- otherwise a
            # worker that hangs BEFORE ever attempting its first tick
            # would never be caught by `is_heartbeat_expired` (no
            # reference timestamp at all yet).
            health.poll_last_started_at = utcnow()

            while not worker_task.done():
                # Correção item 3: `asyncio.wait` retorna assim que a task
                # termina, nunca esperando o intervalo cheio -- uma task
                # morta é detectada quase imediatamente, não só no próximo
                # tique de heartbeat.
                await asyncio.wait({worker_task}, timeout=heartbeat_check_interval)
                expired = is_heartbeat_expired(health, settings.poll_heartbeat_max_age_seconds)
                if expired and not health.heartbeat_alert_active:
                    health.heartbeat_alert_active = True
                    health.status = PollEngineStatus.PARADO
                    orch.engine_degraded = True
                    logger.error("poll_loop_heartbeat_expired")
                    last_success = health.poll_last_success_at.isoformat() if health.poll_last_success_at else "nunca"
                    _record_incident(
                        orch, "POLL_LOOP_HEARTBEAT_EXPIRED",
                        "Heartbeat do motor de mercado vencido -- sem ciclo bem-sucedido recente.",
                        f"último sucesso: {last_success}",
                    )
                elif not expired and health.heartbeat_alert_active:
                    health.heartbeat_alert_active = False

            try:
                exc = worker_task.exception()
            except asyncio.CancelledError:
                return  # shutdown cancelled the worker itself -- nothing to restart

            if exc is None:
                return  # worker returned normally (e.g. REPLAY finished)

            # Correção item 3: unexpected task death, detected and logged
            # immediately -- never silently discovered later via asyncio's
            # own delayed "Task exception was never retrieved" warning.
            health.status = PollEngineStatus.PARADO
            orch.engine_degraded = True
            sanitized = _sanitize_error(exc)
            logger.error("poll_worker_task_died_unexpectedly: %s", sanitized)
            _record_incident(
                orch, "POLL_LOOP_TASK_DIED", "A tarefa do motor de mercado terminou inesperadamente.", sanitized,
            )
            health.restart_count += 1

            await asyncio.sleep(min(backoff, settings.poll_backoff_max_seconds))
            backoff = min(backoff * 2, settings.poll_backoff_max_seconds)
    except asyncio.CancelledError:
        # Correção item 3: shutdown gracioso -- cancela o worker (se ainda
        # vivo) e nunca recria nada durante o encerramento.
        health.status = PollEngineStatus.ENCERRANDO
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown must never hang/crash on this
                logger.exception("poll_worker_shutdown_error")
        raise
