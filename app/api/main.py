"""FastAPI application: wires the whole pipeline together and serves the
dashboard + control API. Boots in REPLAY mode by default (no .env required).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.api import routes_control, routes_dashboard
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


def build_orchestrator(settings) -> Orchestrator:
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

    last_price = {"value": 40000.0}

    def price_provider(symbol: str) -> float:
        return last_price["value"]

    if settings.mode == RunMode.REPLAY:
        market_data_provider = ReplayMarketDataProvider(
            FIXTURES_DIR / "replay_btcusdt.json", symbol=settings.symbol
        )
        execution_engine = PaperLocalExecutionEngine(price_provider=price_provider)
    elif settings.mode == RunMode.PAPER_LOCAL:
        market_data_provider = ReplayMarketDataProvider(
            FIXTURES_DIR / "replay_btcusdt.json", symbol=settings.symbol
        )
        execution_engine = PaperLocalExecutionEngine(price_provider=price_provider)
    else:  # BYBIT_DEMO
        from app.execution.bybit_demo import BybitDemoExecutionEngine
        from app.market_data.bybit_provider import BybitDemoMarketDataProvider

        # Real HTTP wiring for BYBIT_DEMO is intentionally left to
        # docs/OPERACAO_DEMO.md's operational setup step (requires the pybit
        # client + signed requests); the constructors below already validate
        # the base URL is demo/testnet-only and will refuse to start otherwise.
        raise NotImplementedError(
            "BYBIT_DEMO wiring requires a live pybit client per docs/OPERACAO_DEMO.md; "
            "not auto-started to avoid accidental network calls."
        )

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
    )
    orchestrator._last_price_ref = last_price  # noqa: SLF001 - simple test/demo hook
    return orchestrator


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level, settings.log_max_bytes, settings.log_backup_count)
    log_event(logger, 20, "app_starting", mode=settings.mode.value)

    app = FastAPI(title="Agente Trader Demo", version="0.1.0")
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

    @app.on_event("startup")
    async def _start_loop():
        async def loop():
            orch = app.state.orchestrator
            while True:
                result = orch.tick()
                if result.get("status") == "no_data":
                    app.state.replay_done = True
                    log_event(logger, 20, "replay_complete")
                    return
                await asyncio.sleep(0.02)

        app.state.loop_task = asyncio.create_task(loop())

    return app


app = create_app()
