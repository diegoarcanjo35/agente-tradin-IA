"""Fase 2, item 7.7: operational session lifecycle -- one row per execution
session, created or resumed at process startup, ended explicitly on
graceful shutdown. Never mutated by anything outside this module.
"""
from __future__ import annotations

import hashlib
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


def _config_fingerprint(settings, strategy_version: str, risk_limits: RiskLimits) -> str:
    """Correção v1.1 #8: a deterministic SHA-256 over the exact same
    sanitized (never-secret) fields already used for `config_snapshot_json`
    -- strategy version, mode/symbol, timeframe, and every risk limit.
    `json.dumps(sort_keys=True)` makes key order irrelevant, so this is
    stable across process restarts regardless of dict construction order."""
    payload = {
        "strategy_version": strategy_version,
        "timeframe": "1",
        "risk_config": asdict(risk_limits),
        "config_snapshot": _sanitized_config_snapshot(settings),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def start_or_resume_session(
    session: Session, settings, strategy_version: str, risk_limits: RiskLimits,
) -> OperationalSession:
    """Resumes the most recent NOT-ended session for this exact mode+symbol,
    but ONLY if its persisted `config_fingerprint` matches the current
    strategy version/mode/symbol/timeframe/risk config exactly (correção
    v1.1 #8) -- a resumed session can no longer silently keep operating
    under a stale snapshot after a config change. A mismatch ends the old
    session (Portuguese reason) and starts a fresh one; a session with no
    fingerprint at all (a pre-correção-v1.1 row) is treated as a mismatch
    too, never trusted implicitly."""
    fingerprint = _config_fingerprint(settings, strategy_version, risk_limits)

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
        if existing.config_fingerprint == fingerprint:
            return existing
        end_session(
            session, existing,
            "Configuração alterada (estratégia/risco/timeframe) -- sessão anterior encerrada, "
            "nova sessão iniciada com o novo fingerprint.",
        )

    op_session = OperationalSession(
        session_uid=str(uuid.uuid4()),
        mode=settings.mode.value,
        symbol=settings.symbol,
        timeframe="1",
        strategy_version=strategy_version,
        risk_config_json=json.dumps(asdict(risk_limits)),
        config_snapshot_json=json.dumps(_sanitized_config_snapshot(settings)),
        config_fingerprint=fingerprint,
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
