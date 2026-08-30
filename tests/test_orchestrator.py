"""End-to-end REPLAY smoke test: market data -> strategy -> risk -> execution
-> persistence, driven purely by the local fixture (no .env, no network).
"""
from __future__ import annotations

from pathlib import Path

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.core.config import RunMode, Settings
from app.execution.paper_local import PaperLocalExecutionEngine
from app.market_data.replay_provider import ReplayMarketDataProvider
from app.orchestrator import Orchestrator
from app.persistence import repo
from app.persistence.db import session_scope
from app.risk.engine import RiskEngine
from app.risk.config import RiskLimits
from app.strategy.engine import StrategyEngine

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "replay_btcusdt.json"


def build_orchestrator(session_factory) -> Orchestrator:
    settings = Settings(mode=RunMode.REPLAY)
    market_data_provider = ReplayMarketDataProvider(FIXTURE, symbol=settings.symbol)
    strategy_engine = StrategyEngine(symbol=settings.symbol)
    risk_engine = RiskEngine(RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0))
    last_price = {"value": 40000.0}
    execution_engine = PaperLocalExecutionEngine(price_provider=lambda s: last_price["value"])
    ai_agent = AIShadowAgent(provider=SimulatedProvider(), timeout_seconds=2.0)
    return Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=market_data_provider, strategy_engine=strategy_engine,
        risk_engine=risk_engine, execution_engine=execution_engine, ai_agent=ai_agent,
    )


def test_full_replay_run_produces_traceable_records(session_factory):
    orch = build_orchestrator(session_factory)
    results = []
    for _ in range(500):
        result = orch.tick()
        results.append(result)
        if result["status"] == "no_data":
            break

    assert results[-1]["status"] == "no_data"

    with session_scope(session_factory) as session:
        signals = repo.recent_signals(session, limit=1000)
        ai_recs = repo.recent_ai_recommendations(session, limit=1000)
        risk_evals = repo.recent_risk_evaluations(session, limit=1000)
        assert len(signals) > 0
        assert len(ai_recs) > 0
        # Every risk evaluation must trace back to a real signal id.
        signal_ids = {s.id for s in signals}
        for ev in risk_evals:
            assert ev.signal_id in signal_ids


def test_kill_switch_blocks_new_orders(session_factory):
    orch = build_orchestrator(session_factory)
    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = True
        state.trading_blocked = True

    approvals = []
    for _ in range(300):
        result = orch.tick()
        if result["status"] == "no_data":
            break
        if result["status"] not in ("hold", "position_closed"):
            approvals.append(result)

    # With the kill switch engaged from tick 0, no order should ever be filled.
    assert all(r["status"] != "order_filled" for r in approvals)
