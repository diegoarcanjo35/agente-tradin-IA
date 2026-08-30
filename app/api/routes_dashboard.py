from __future__ import annotations

import json

from fastapi import APIRouter, Request

from app.metrics.engine import ClosedTrade, compute_metrics
from app.persistence import repo
from app.persistence.db import session_scope

router = APIRouter()


@router.get("/state")
def get_state(request: Request):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        return {
            "mode": orch.settings.mode.value,
            "trading_blocked": state.trading_blocked,
            "block_reason": state.block_reason,
            "kill_switch_engaged": state.kill_switch_engaged,
            "consecutive_losses": state.consecutive_losses,
            "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
            "api_failure_count": state.api_failure_count,
            "replay_done": request.app.state.replay_done,
            "environment_banner": "AMBIENTE DEMO — SEM DINHEIRO REAL",
        }


@router.get("/metrics")
def get_metrics(request: Request):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        closed = repo.closed_positions(session)
        trades = [
            ClosedTrade(
                realized_pnl=p.realized_pnl, fees_paid=p.fees_paid,
                opened_at=p.opened_at, closed_at=p.closed_at,
            )
            for p in closed
            if p.closed_at is not None
        ]
        open_pos = repo.open_positions(session)
        open_exposure = sum(p.qty * p.avg_entry_price for p in open_pos)
        result = compute_metrics(trades, starting_balance=1000.0, open_exposure_usd=open_exposure)
        return result.__dict__


@router.get("/positions")
def get_positions(request: Request):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        open_pos = repo.open_positions(session)
        return [
            {
                "id": p.id, "symbol": p.symbol, "side": p.side, "qty": p.qty,
                "avg_entry_price": p.avg_entry_price, "stop_loss": p.stop_loss,
                "take_profit": p.take_profit, "opened_at": p.opened_at.isoformat(),
            }
            for p in open_pos
        ]


@router.get("/equity-curve")
def get_equity_curve(request: Request):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        closed = sorted(
            [p for p in repo.closed_positions(session) if p.closed_at],
            key=lambda p: p.closed_at,
        )
        running = 1000.0
        points = [{"t": None, "equity": running}]
        for p in closed:
            running += p.realized_pnl - p.fees_paid
            points.append({"t": p.closed_at.isoformat(), "equity": running})
        return points


@router.get("/signals")
def get_signals(request: Request, limit: int = 50):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_signals(session, limit=limit)
        return [
            {
                "id": r.id, "symbol": r.symbol, "direction": r.direction,
                "justification": r.justification, "observed_price": r.observed_price,
                "atr": r.atr, "params": json.loads(r.params_json),
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.get("/ai-recommendations")
def get_ai_recommendations(request: Request, limit: int = 50):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_ai_recommendations(session, limit=limit)
        return [
            {
                "id": r.id, "symbol": r.symbol, "signal_id": r.signal_id,
                "recommendation": r.recommendation, "confidence": r.confidence,
                "reasoning_summary": r.reasoning_summary,
                "risk_flags": json.loads(r.risk_flags_json), "provider": r.provider,
                "model_version": r.model_version, "is_valid": r.is_valid,
                "rejection_reason": r.rejection_reason, "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.get("/risk-evaluations")
def get_risk_evaluations(request: Request, limit: int = 50):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_risk_evaluations(session, limit=limit)
        return [
            {
                "id": r.id, "signal_id": r.signal_id, "approved": r.approved,
                "reason": r.reason, "checks": json.loads(r.checks_json),
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.get("/security-events")
def get_security_events(request: Request, limit: int = 50):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_security_events(session, limit=limit)
        return [
            {"id": r.id, "event_type": r.event_type, "detail": r.detail,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ]


@router.get("/failures")
def get_failures(request: Request, limit: int = 50):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        rows = repo.recent_failures(session, limit=limit)
        return [
            {"id": r.id, "kind": r.kind, "detail": r.detail, "resolved": r.resolved,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ]
