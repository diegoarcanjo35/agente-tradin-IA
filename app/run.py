"""Correction v1.3 #3, opção A: ponto de entrada oficial e único suportado
para iniciar o servidor. Sempre lê `host`/`port` de `Settings` (já validado
por `Settings.assert_safe_bind_host()`), nunca de um argumento de linha de
comando solto -- eliminando a divergência que permitia declarar proteção de
bind sem que ela controlasse o bind real (`uvicorn app.api.main:app --host
0.0.0.0` passava batido pela validação em `Settings`, que só olha a variável
de ambiente interna, nunca o host efetivamente usado pelo servidor).

Uso:
    python -m app.run

Não existe (nem deve ser criado) um segundo caminho suportado para iniciar o
servidor em produção/demonstração. Ver docs/OPERACAO_DEMO.md e
docs/SEGURANCA.md.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger, log_event

logger = get_logger(__name__)


def run() -> None:
    import uvicorn

    settings = get_settings()  # already raises UnsafeBindHostError if unsafe
    log_event(
        logger, 20, "launcher_starting",
        host=settings.api_host, port=settings.api_port, mode=settings.mode.value,
    )
    # Correção Operacional do Poll Loop v1.2, Bloqueio 1: passes uvicorn the
    # FACTORY (`create_app`), not a pre-built module-level `app` object --
    # `app/api/main.py` no longer instantiates one at import time (importing
    # it must never itself open a database/run a reconciliation/spawn the
    # poll loop). `factory=True` tells uvicorn to import the module (a pure,
    # side-effect-free import) and call `create_app()` itself, exactly once,
    # to build the real application.
    uvicorn.run(
        "app.api.main:create_app",
        host=settings.api_host,
        port=settings.api_port,
        factory=True,
    )


if __name__ == "__main__":
    run()
