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
from app.core.clock import ReplayClockProvider
from app.core.config import RunMode, get_settings
from app.core.logging import get_logger, log_event, setup_logging
from app.execution.paper_local import PaperLocalExecutionEngine
from app.market_data.replay_provider import ReplayMarketDataProvider
from app.orchestrator import Orchestrator
from app.persistence.db import init_db, make_engine, make_session_factory, session_scope
from app.persistence import repo
from app.risk.engine import RiskEngine
from app.risk.config import RiskLimits
from app.strategy.engine import StrategyEngine

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
            )
            transport = PybitTransport(pybit_client)

        market_data_provider = BybitDemoMarketDataProvider(
            settings.bybit_base_url, settings.symbol, "1", http_get=transport.http_get,
        )
        execution_engine = BybitDemoExecutionEngine(
            settings.bybit_base_url, http_post=transport.http_post, http_get=transport.http_get,
        )
        clock_provider = BybitServerTimeProvider(settings.bybit_base_url, http_get=transport.http_get)

    ai_agent = AIShadowAgent(
        provider=SimulatedProvider(),
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
    )

    # Startup/post-restart reconciliation (correction 8): runs for every
    # mode. For REPLAY/PAPER_LOCAL there are no persisted open positions on a
    # fresh DB, so this is a fast no-op; for BYBIT_DEMO it is the first real
    # network call the process makes, and any mismatch or failure blocks
    # trading immediately rather than trusting stale local state.
    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orchestrator.reconcile(session, state)

    return orchestrator


async def _poll_loop(app: FastAPI) -> None:
    """Correction v1.2 #1: only CandleFetchStatus.REPLAY_FINISHED (surfaced
    by Orchestrator.tick() as status "no_data") may end this loop. Every
    other outcome -- no new candle yet, a retryable error, a fatal error --
    keeps polling; retryable/fatal errors just feed TRADING_BLOCKED via the
    orchestrator instead of silently killing the background task."""
    orch = app.state.orchestrator
    settings = app.state.settings
    interval = (
        settings.bybit_poll_interval_seconds
        if settings.mode == RunMode.BYBIT_DEMO
        else settings.replay_poll_interval_seconds
    )
    while True:
        result = orch.tick()
        status = result.get("status")
        if status == "no_data":
            app.state.replay_done = True
            log_event(logger, 20, "replay_complete")
            return
        if status == "retryable_error":
            log_event(logger, 30, "market_data_retryable_error", detail=result.get("detail"))
        elif status == "fatal_error":
            log_event(logger, 40, "market_data_fatal_error", detail=result.get("detail"))
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.loop_task = asyncio.create_task(_poll_loop(app))
    try:
        yield
    finally:
        app.state.loop_task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level, settings.log_max_bytes, settings.log_backup_count)
    log_event(logger, 20, "app_starting", mode=settings.mode.value)

    app = FastAPI(title="Agente Trader Demo", version="0.1.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.orchestrator = build_orchestrator(settings)
    app.state.replay_done = False
    app.state.loop_task = None

    app.include_router(routes_dashboard.router, prefix="/api")
    app.include_router(routes_control.router, prefix="/api")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    return app


app = create_app()
