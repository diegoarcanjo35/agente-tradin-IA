"""FastAPI application: wires the whole pipeline together and serves the
dashboard + control API. Boots in REPLAY mode by default (no .env required).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.api import routes_control, routes_dashboard
from app.api.poll_engine import PollHealth, supervise_poll_loop, wait_for_in_flight_tick_before_shutdown
from app.core.clock import ReplayClockProvider
from app.core.config import RunMode, get_settings
from app.core.logging import get_logger, log_event, setup_logging
from app.execution.paper_local import PaperLocalExecutionEngine
from app.market_data.replay_provider import ReplayMarketDataProvider
from app.orchestrator import Orchestrator
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence import repo
from app.persistence.models import OperationalSession
from app.risk.engine import RiskEngine
from app.risk.config import RiskLimits
from app.sessions import end_session, start_or_resume_session
from app.strategy.engine import StrategyEngine

# Correção Cirúrgica do Stop/Take Pós-Preenchimento: v1 -> v1.1 muda o
# config_fingerprint (app/sessions.py::_config_fingerprint inclui
# strategy_version) -- no próximo boot, start_or_resume_session encerra
# graciosamente a sessão anterior (regra antiga: stop/take do preço do
# sinal) e cria uma nova (regra nova: stop/take do preço de preenchimento),
# permitindo comparar os dois períodos sem misturar resultados.
STRATEGY_VERSION = "v1.1"

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = BASE_DIR / "fixtures"
FRONTEND_DIR = BASE_DIR / "frontend"

logger = get_logger(__name__)


def build_orchestrator(settings, bybit_transport=None) -> Orchestrator:
    """`bybit_transport`, when given, replaces the real pybit-backed
    transport used in BYBIT_DEMO mode with an object exposing the same
    `http_get(url, params)` / `http_post(url, payload)` interface (see
    tests/fakes/bybit_fake.py::FakeBybitTransport). This exists purely so
    tests can exercise this function's REAL wiring logic -- mode branching,
    engine/provider construction, clock provider selection, startup
    reconciliation -- against BYBIT_DEMO without ever importing pybit or
    touching the network. Production code never passes this argument."""
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    strategy_engine = StrategyEngine(symbol=settings.symbol)
    risk_limits = RiskLimits(
        max_position_usd=settings.risk_max_position_usd,
        max_concurrent_positions=settings.risk_max_concurrent_positions,
        max_daily_loss_usd=settings.risk_max_daily_loss_usd,
        max_total_exposure_usd=settings.risk_max_total_exposure_usd,
        cooldown_after_losses=settings.risk_cooldown_after_losses,
        cooldown_minutes=settings.risk_cooldown_minutes,
        max_data_staleness_seconds=settings.risk_max_data_staleness_seconds,
        max_api_failures=settings.risk_max_api_failures,
        max_clock_drift_seconds=settings.risk_max_clock_drift_seconds,
    )
    risk_engine = RiskEngine(limits=risk_limits)

    # Single source of truth for "the price of the candle currently driving
    # the decision" -- the orchestrator writes it every tick; PAPER_LOCAL's
    # price_provider only ever falls back to it if a caller forgets to pass
    # an explicit reference_price (the orchestrator always does).
    price_state: dict[str, float] = {}

    def price_provider(symbol: str) -> float:
        return price_state.get(symbol, 0.0)

    # Correção v1.1 #6: None for every mode except BYBIT_DEMO (set below) --
    # None means app.metrics.engine reports funding as UNAVAILABLE rather
    # than a fabricated/simulated value.
    funding_provider = None

    if settings.mode == RunMode.REPLAY:
        market_data_provider = ReplayMarketDataProvider(
            FIXTURES_DIR / "replay_btcusdt.json", symbol=settings.symbol
        )
        execution_engine = PaperLocalExecutionEngine(price_provider=price_provider)
        clock_provider = ReplayClockProvider(drift_seconds=0.0)
    elif settings.mode == RunMode.PAPER_LOCAL:
        market_data_provider = ReplayMarketDataProvider(
            FIXTURES_DIR / "replay_btcusdt.json", symbol=settings.symbol
        )
        execution_engine = PaperLocalExecutionEngine(price_provider=price_provider)
        clock_provider = ReplayClockProvider(drift_seconds=0.0)
    elif settings.mode == RunMode.PAPER_LIVE:
        # Fase 2, item 7.1: REAL Bybit Demo market data (public endpoints
        # only), execution stays entirely local/simulated. Deliberately
        # never calls require_bybit_credentials(), never builds an
        # authenticated pybit client or http_post, and never constructs
        # BybitDemoExecutionEngine -- PAPER_LIVE cannot reach the exchange's
        # private order-management endpoints even if this code had a bug,
        # because PaperLocalExecutionEngine (below) simply has no code path
        # that calls http_post at all.
        from app.execution.bybit_pybit_client import PybitTransport, build_public_pybit_client
        from app.market_data.bybit_provider import BybitDemoMarketDataProvider, BybitServerTimeProvider

        if bybit_transport is not None:
            transport = bybit_transport
        else:
            pybit_client = build_public_pybit_client(
                settings.bybit_base_url, settings.bybit_ws_url,
                timeout_seconds=settings.bybit_http_timeout_seconds,
            )
            transport = PybitTransport(pybit_client)

        market_data_provider = BybitDemoMarketDataProvider(
            settings.bybit_base_url, settings.symbol, "1", http_get=transport.http_get,
            initial_start=settings.market_data_initial_start,
        )
        # Correção v1.1 #5: the configured fee/slippage are genuinely wired
        # here, not left as PaperLocalExecutionEngine's own hardcoded
        # defaults -- every fill this engine produces mathematically
        # reflects settings.paper_live_fee_rate/paper_live_slippage_bps.
        execution_engine = PaperLocalExecutionEngine(
            price_provider=price_provider,
            fee_rate=settings.paper_live_fee_rate,
            slippage_bps=settings.paper_live_slippage_bps,
        )
        clock_provider = BybitServerTimeProvider(settings.bybit_base_url, http_get=transport.http_get)
    else:  # BYBIT_DEMO
        from app.execution.bybit_demo import BybitDemoExecutionEngine
        from app.execution.bybit_pybit_client import PybitTransport, build_pybit_client
        from app.market_data.bybit_provider import BybitDemoMarketDataProvider, BybitServerTimeProvider

        # require_bybit_credentials() already ran inside get_settings() for
        # BYBIT_DEMO, before this function is ever called -- re-checked here
        # defensively so build_orchestrator() is safe to call directly too.
        # This must happen BEFORE any client/transport is built, so a missing
        # credential fails before a single network call is even possible.
        settings.require_bybit_credentials()

        if bybit_transport is not None:
            transport = bybit_transport
        else:
            pybit_client = build_pybit_client(
                settings.bybit_base_url, settings.bybit_ws_url,
                settings.bybit_api_key, settings.bybit_api_secret,
                timeout_seconds=settings.bybit_http_timeout_seconds,
            )
            transport = PybitTransport(pybit_client)

        market_data_provider = BybitDemoMarketDataProvider(
            settings.bybit_base_url, settings.symbol, "1", http_get=transport.http_get,
            initial_start=settings.market_data_initial_start,
        )
        execution_engine = BybitDemoExecutionEngine(
            settings.bybit_base_url, http_post=transport.http_post, http_get=transport.http_get,
        )
        clock_provider = BybitServerTimeProvider(settings.bybit_base_url, http_get=transport.http_get)

        # Correção v1.1 #6: funding is only ever collected for BYBIT_DEMO --
        # the sole mode with real private-endpoint credentials. PAPER_LIVE
        # (which reaches this same public-transport branch above) and
        # REPLAY/PAPER_LOCAL never get a funding_provider at all, so
        # app.metrics.engine reports funding as UNAVAILABLE for them,
        # rather than a simulated value mixed in with real collected data.
        from app.execution.funding import BybitFundingProvider

        funding_provider = BybitFundingProvider(transport.http_get, settings.bybit_base_url)

    # Correção v1.1 #5: SimulatedProvider stays the default in every case;
    # only a deliberate, fully-configured opt-in (toggle ON AND both the
    # API key and endpoint URL actually set) swaps in the external
    # provider -- a half-configured toggle silently falls back to
    # SimulatedProvider rather than failing or reaching out with an empty
    # key/url.
    ai_provider = SimulatedProvider()
    if settings.ai_shadow_external_provider_enabled and settings.ai_provider_api_key and settings.ai_provider_endpoint_url:
        from app.ai_shadow.http_provider import HttpAIProvider

        ai_provider = HttpAIProvider(
            endpoint_url=settings.ai_provider_endpoint_url, api_key=settings.ai_provider_api_key,
        )

    ai_agent = AIShadowAgent(
        provider=ai_provider,
        timeout_seconds=settings.ai_timeout_seconds,
        max_response_chars=settings.ai_max_response_chars,
        enabled=settings.ai_shadow_enabled_default,
    )

    orchestrator = Orchestrator(
        settings=settings,
        session_factory=session_factory,
        market_data_provider=market_data_provider,
        strategy_engine=strategy_engine,
        risk_engine=risk_engine,
        execution_engine=execution_engine,
        ai_agent=ai_agent,
        clock_provider=clock_provider,
        price_state=price_state,
        funding_provider=funding_provider,
    )

    # Startup/post-restart reconciliation (correction 8): runs for every
    # mode. For REPLAY/PAPER_LOCAL there are no persisted open positions on a
    # fresh DB, so this is a fast no-op; for BYBIT_DEMO it is the first real
    # network call the process makes, and any mismatch or failure blocks
    # trading immediately rather than trusting stale local state.
    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)

        # Fase 2, item 7.7: create or resume the operational session for
        # this exact mode+symbol BEFORE the startup reconciliation below, so
        # that reconciliation is itself counted in reconciliations_count.
        # Item 7.8: a session/process only ever comes up as OBSERVANDO
        # (monitoring, reconciling, able to close/reduce exposure) -- never
        # ATIVO. Opening new entries always requires an explicit
        # POST /operational-state/activate afterward, regardless of mode or
        # how clean the startup reconciliation was.
        op_session = start_or_resume_session(session, settings, STRATEGY_VERSION, risk_limits)
        state.active_session_id = op_session.id

        orchestrator.reconcile(session, state)

        state.operational_state = "BLOQUEADO" if state.trading_blocked else "OBSERVANDO"
        op_session.status = state.operational_state

    return orchestrator


async def _graceful_shutdown(app: FastAPI) -> None:
    """Correção v1.1 #7: `end_session()` existed but was dead code -- the
    old shutdown only cancelled the poll task, leaving the operational
    session open forever and the process' final state unrecorded. Now:
    blocks new entries (`operational_state=ENCERRANDO`), stops the loop
    cleanly (awaiting the CancelledError, never leaving it dangling), runs
    one last reconciliation (never lets a failure there hang or crash
    shutdown), and ends the session with `ended_at`/a Portuguese reason --
    all persisted before returning, so a genuine crash (which never
    reaches this function) is the only path that leaves a session
    resumable on the next boot."""
    orch = app.state.orchestrator

    loop_task = app.state.loop_task
    if loop_task is not None:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - shutdown must never hang/crash on this
            log_event(logger, 40, "poll_loop_shutdown_error", detail=str(exc))

    # Correção Operacional do Poll Loop v1.1, item 3: cancelling the
    # supervisor/worker Tasks above does NOT kill an in-flight `orch.tick()`
    # running inside its dedicated executor thread -- a Python thread can't
    # be forcibly cancelled. Without this, that tick could still commit a
    # write to the database AFTER the reconciliation/end_session below
    # already ran. This is an absolute wait (never abandons it), so a
    # genuinely stuck tick blocks shutdown rather than risking a late
    # mutation -- see `wait_for_in_flight_tick_before_shutdown`'s
    # docstring for the explicit, testable warning behavior.
    await wait_for_in_flight_tick_before_shutdown(app)

    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.operational_state = "ENCERRANDO"

        try:
            orch.reconcile(session, state)
        except Exception as exc:  # noqa: BLE001 - shutdown must never hang/crash on this
            log_event(logger, 40, "shutdown_reconciliation_failed", detail=str(exc))

        if state.active_session_id is not None:
            op_session = session.get(OperationalSession, state.active_session_id)
            if op_session is not None and op_session.ended_at is None:
                end_session(session, op_session, "Encerramento gracioso do processo.")

    log_event(logger, 20, "app_shutdown_complete")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Correção operacional do poll loop v1.0: the task started here is now
    # the SUPERVISOR (app/api/poll_engine.py::supervise_poll_loop), not the
    # raw tick loop directly -- it owns creating/restarting/cancelling the
    # actual worker task internally. `app.state.loop_task` keeps its name
    # for the shutdown code below, which only needs "the one task to
    # cancel and await" and doesn't care which coroutine that is.
    app.state.loop_task = asyncio.create_task(supervise_poll_loop(app), name="poll-supervisor")
    try:
        yield
    finally:
        await _graceful_shutdown(app)


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level, settings.log_max_bytes, settings.log_backup_count)
    log_event(logger, 20, "app_starting", mode=settings.mode.value)

    app = FastAPI(title="Agente Trader Demo", version="0.1.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.orchestrator = build_orchestrator(settings)
    app.state.replay_done = False
    app.state.loop_task = None
    # Correção operacional do poll loop v1.0: process-memory health state,
    # shared between the supervisor/worker (which update it) and
    # /api/state + the activation endpoint (which read it). See
    # app/api/poll_engine.py::PollHealth's docstring for why this is never
    # persisted to the database.
    app.state.poll_health = PollHealth()
    app.state.poll_worker_task = None
    app.state.poll_in_flight_future = None

    app.include_router(routes_dashboard.router, prefix="/api")
    app.include_router(routes_control.router, prefix="/api")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    return app


# Correção Operacional do Poll Loop v1.2, Bloqueio 1: NO module-level
# `app = create_app()` -- importing this module (or `create_app`, or any
# route/helper from it) must never itself open a database, run migrations,
# create/resume an operational session, run a reconciliation, spawn the
# poll supervisor task, or touch any external client. Only a DELIBERATE
# call to `create_app()` does any of that. Production starts the server
# via `app/run.py`, which passes uvicorn the factory itself
# (`"app.api.main:create_app"`, `factory=True`) -- uvicorn imports this
# module (a pure import, no side effects) and only THEN calls
# `create_app()` once to build the actual application.
