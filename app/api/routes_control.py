"""Control endpoints. Deliberately does NOT include any mode/environment
toggle -- mode is process-level config only (see app/core/config.py).

Correction v1.3 #3 / v1.4 #4 (opções B+C combinadas): a validação de
`Settings.api_host` só é confiável quando o processo foi iniciado pelo
launcher oficial (`app/run.py`) -- um `uvicorn app.api.main:app --host
0.0.0.0` direto contorna completamente esse controle, e a aplicação não tem
como observar ou impedir isso a partir de dentro do processo ASGI. Como
segunda camada, independente de como o servidor foi iniciado:

- ATIVAR o bloqueio de emergência nunca exige autenticação -- é uma ação que
  só aumenta a segurança, então liberá-la de qualquer origem é seguro
  (opção C: "permitir ativar pela interface").
- DESATIVAR o bloqueio de emergência (liberar operações) LOCALMENTE
  (127.0.0.1/::1) sempre funciona, COM ou SEM `CONTROL_API_TOKEN`
  configurado -- correção v1.4 #4: uma versão anterior exigia o token
  mesmo para requisições locais assim que ele era configurado, o que
  quebrava o próprio painel local ao ativar a proteção para acesso remoto.
  O painel nunca precisa (nem deve) conhecer o token.
- DESATIVAR remotamente exige `X-Control-Token` batendo com
  `CONTROL_API_TOKEN`; sem token configurado, acesso remoto é negado por
  padrão.
- A origem é sempre `request.client.host`, o endereço do socket TCP que o
  servidor ASGI realmente aceitou -- nunca um cabeçalho como
  `X-Forwarded-For`/`X-Real-IP`, que qualquer cliente pode forjar. Atrás de
  um proxy reverso, `request.client.host` será o endereço do PRÓPRIO proxy,
  não do usuário final; nesse cenário configure `CONTROL_API_TOKEN` -- não
  há aqui nenhuma lista de proxies confiáveis para repassar cabeçalhos de
  origem.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request

from app.persistence import repo
from app.persistence.db import session_scope

router = APIRouter()

LOCAL_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "testclient"})


def require_local_or_authenticated(request: Request) -> None:
    """Dependency guarding any control action that REDUCES safety (i.e.
    releases operations).

    Policy (correction v1.4 #4): a LOCAL client is always allowed, whether
    or not CONTROL_API_TOKEN is configured -- configuring the token
    protects remote access; it must never lock out the local panel. A
    non-local client is denied unless it presents the correct token; with
    no token configured at all, remote access is denied outright (deny by
    default, never silently open).
    """
    client_host = request.client.host if request.client is not None else None
    if client_host in LOCAL_CLIENT_HOSTS:
        return

    settings = request.app.state.settings
    token = getattr(settings, "control_api_token", "") or ""
    if not token:
        raise HTTPException(
            status_code=403,
            detail=(
                "Acesso negado: nenhum CONTROL_API_TOKEN configurado e a requisição não "
                "é local. Desativar o bloqueio de emergência remotamente exige um token "
                "de controle configurado via CONTROL_API_TOKEN."
            ),
        )

    provided = request.headers.get("X-Control-Token", "")
    if hmac.compare_digest(provided, token):
        return
    raise HTTPException(
        status_code=403,
        detail="Token de controle inválido ou ausente. Desativação recusada.",
    )


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
    devolvido em português -- nunca liberado por engano.

    Correction v1.3 #3: exige origem local ou token de controle válido
    (ver require_local_or_authenticated) antes de liberar qualquer coisa."""
    require_local_or_authenticated(request)

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
