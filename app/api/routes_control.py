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
        repo.recompute_trading_blocked(state, orch.settings.risk_max_api_failures)
        repo.record_security_event(
            session, "KILL_SWITCH_ENGAGED", "Ativação manual do bloqueio de emergência pelo painel."
        )
        return {
            "kill_switch_engaged": True,
            "trading_blocked": state.trading_blocked,
            "mensagem": "Bloqueio de emergência ativado. Nenhuma nova operação será realizada.",
        }


@router.post("/kill-switch/disengage")
def disengage_kill_switch(request: Request):
    """Correction v1.2 #5: desengatar o kill switch remove SOMENTE o
    bloqueio manual. Se houver qualquer outro motivo de bloqueio ativo
    (reconciliação divergente/estado ambíguo, relógio fora de sincronia,
    limite de falhas de API), o sistema permanece bloqueado e o motivo é
    devolvido em português -- nunca liberado por engano."""
    orch = request.app.state.orchestrator
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        state.kill_switch_engaged = False
        repo.recompute_trading_blocked(state, orch.settings.risk_max_api_failures)

        if state.trading_blocked:
            repo.record_security_event(
                session, "KILL_SWITCH_DISENGAGED_OUTROS_BLOQUEIOS_ATIVOS",
                f"Bloqueio de emergência desativado, mas as operações continuam bloqueadas: {state.block_reason}",
            )
            return {
                "kill_switch_engaged": False,
                "trading_blocked": True,
                "mensagem": (
                    "Bloqueio de emergência desativado, mas as operações continuam "
                    f"bloqueadas por outro motivo: {state.block_reason}."
                ),
            }

        repo.record_security_event(
            session, "KILL_SWITCH_DISENGAGED", "Desativação manual do bloqueio de emergência pelo painel."
        )
        return {
            "kill_switch_engaged": False,
            "trading_blocked": False,
            "mensagem": "Bloqueio de emergência desativado. Operações liberadas.",
        }
