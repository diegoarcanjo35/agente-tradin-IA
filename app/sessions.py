"""Fase 2, item 7.7: operational session lifecycle -- one row per execution
session, created or resumed at process startup, ended explicitly on
graceful shutdown. Never mutated by anything outside this module.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.persistence.models import OperationalSession
from app.risk.config import RiskLimits


def _sanitized_config_snapshot(settings) -> dict:
    """Explicit ALLOWLIST of fields (never a blocklist) -- new Settings
    fields are excluded by default until deliberately added here, so a
    secret added later can never leak into a session snapshot by accident."""
    snapshot = {
        "mode": settings.mode.value,
        "symbol": settings.symbol,
        "risk_max_position_usd": settings.risk_max_position_usd,
        "risk_max_concurrent_positions": settings.risk_max_concurrent_positions,
        "risk_max_daily_loss_usd": settings.risk_max_daily_loss_usd,
        "risk_max_total_exposure_usd": settings.risk_max_total_exposure_usd,
        "risk_cooldown_after_losses": settings.risk_cooldown_after_losses,
        "risk_cooldown_minutes": settings.risk_cooldown_minutes,
        "reconciliation_interval_seconds": settings.reconciliation_interval_seconds,
        "reconciliation_max_delay_seconds": settings.reconciliation_max_delay_seconds,
        "ai_shadow_enabled_default": settings.ai_shadow_enabled_default,
    }
    if settings.mode.value != "REPLAY":
        # The base URL is not a secret (it's the allowlisted demo host,
        # already validated) -- api_key/api_secret are never included here.
        snapshot["bybit_base_url"] = settings.bybit_base_url
    return snapshot


def start_or_resume_session(
    session: Session, settings, strategy_version: str, risk_limits: RiskLimits,
) -> OperationalSession:
    """Resumes the most recent NOT-ended session for this exact mode+symbol,
    or creates a new one. Never resumes across modes/symbols -- a session is
    meaningless if the process is now observing a different market or
    running in a different mode than when it started."""
    existing = session.execute(
        select(OperationalSession)
        .where(
            OperationalSession.mode == settings.mode.value,
            OperationalSession.symbol == settings.symbol,
            OperationalSession.ended_at.is_(None),
        )
        .order_by(OperationalSession.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    op_session = OperationalSession(
        session_uid=str(uuid.uuid4()),
        mode=settings.mode.value,
        symbol=settings.symbol,
        timeframe="1",
        strategy_version=strategy_version,
        risk_config_json=json.dumps(asdict(risk_limits)),
        config_snapshot_json=json.dumps(_sanitized_config_snapshot(settings)),
        status="INICIALIZANDO",
    )
    session.add(op_session)
    session.flush()
    return op_session


def end_session(session: Session, op_session: OperationalSession, reason: str) -> None:
    op_session.ended_at = utcnow()
    op_session.end_reason = reason
    op_session.status = "ENCERRANDO"
    session.flush()


# --- Counters (Fase 2, item 7.7) --------------------------------------------
# Incremented from app/orchestrator.py at the exact points each event is
# already known to have happened -- never re-derived by a separate query, so
# there is no risk of double-counting or drifting from what actually ran.

def increment(op_session: OperationalSession | None, field: str, by: int = 1) -> None:
    if op_session is None:
        return
    setattr(op_session, field, getattr(op_session, field) + by)
