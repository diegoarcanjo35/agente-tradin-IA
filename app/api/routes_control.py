"""Control endpoints. Deliberately does NOT include any mode/environment
toggle -- mode is process-level config only (see app/core/config.py)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.persistence import repo
from app.persistence.db import session_scope

router = APIRouter()


@router.post("/kill-switch/engage")
def engage_kill_switch(request: Request):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = True
        state.trading_blocked = True
        state.block_reason = "Kill switch engaged manually via dashboard."
        repo.record_security_event(session, "KILL_SWITCH_ENGAGED", "Manual kill switch activation.")
        return {"kill_switch_engaged": True, "trading_blocked": True}


@router.post("/kill-switch/disengage")
def disengage_kill_switch(request: Request):
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = False
        state.trading_blocked = False
        state.block_reason = None
        repo.record_security_event(session, "KILL_SWITCH_DISENGAGED", "Manual kill switch deactivation.")
        return {"kill_switch_engaged": False, "trading_blocked": False}
