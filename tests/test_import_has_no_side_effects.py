"""Correção Operacional do Poll Loop v1.2, Bloqueio 1: reprodução auditada
-- `import app.api.main` (ou qualquer submódulo/rota dele) costumava
instanciar `app = create_app()` no nível do módulo, o que abria um banco
SQLite, rodava migrations, criava/retomava uma sessão operacional e uma
reconciliação -- tudo isso só por causa de uma importação, sem nenhuma
intenção de "ligar" o sistema. Foi exatamente essa classe de defeito que
tocou `agente_trader_paper_live.db` por acidente numa checagem de sintaxe
durante a correção v1.1.

Estes testes rodam em SUBPROCESSO isolado (não apenas `importlib.reload`
dentro do próprio processo do pytest) porque o efeito real (arquivo de
banco criado no disco) só é uma prova confiável quando o processo Python
inteiro é novo -- exatamente como o incidente real aconteceu.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_subprocess(code: str, database_url: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MODE"] = "REPLAY"
    env["DATABASE_URL"] = database_url
    # Nunca herda credenciais reais do ambiente do desenvolvedor, mesmo que
    # existam -- este teste nunca deveria alcançar nenhum caminho que as
    # use, mas por segurança nunca as propaga para o subprocesso.
    env.pop("BYBIT_API_KEY", None)
    env.pop("BYBIT_API_SECRET", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_importing_app_api_main_creates_no_database_file(tmp_path):
    """Reprodução auditada exata: MODE=REPLAY, DATABASE_URL apontando para
    um arquivo inexistente, só `import app.api.main` -- o arquivo deve
    continuar inexistente depois."""
    db_path = tmp_path / "should_never_be_created.db"
    assert not db_path.exists()

    result = _run_subprocess("import app.api.main", database_url=f"sqlite:///{db_path}")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert not db_path.exists(), "importar app.api.main criou um banco -- efeito colateral de importação"


def test_importing_create_app_itself_creates_no_database_file(tmp_path):
    """Importar a própria factory (sem CHAMAR ela) também não pode ter
    efeito nenhum."""
    db_path = tmp_path / "should_never_be_created_2.db"
    result = _run_subprocess("from app.api.main import create_app", database_url=f"sqlite:///{db_path}")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert not db_path.exists()


def test_importing_routes_and_helpers_creates_no_database_file(tmp_path):
    db_path = tmp_path / "should_never_be_created_3.db"
    code = (
        "from app.api import routes_control, routes_dashboard\n"
        "from app.api.main import build_orchestrator\n"
    )
    result = _run_subprocess(code, database_url=f"sqlite:///{db_path}")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert not db_path.exists()


def test_module_no_longer_exposes_a_prebuilt_app_at_import_time(tmp_path):
    """Confirma a mudança de desenho diretamente: `app.api.main` não pode
    ter um atributo `app` (FastAPI já construído) logo após a importação --
    só `create_app` (a função) deve existir."""
    db_path = tmp_path / "should_never_be_created_4.db"
    code = (
        "import app.api.main as m\n"
        "assert not hasattr(m, 'app'), 'app.api.main.app existe no nível do módulo -- regressão do Bloqueio 1'\n"
        "assert callable(m.create_app)\n"
    )
    result = _run_subprocess(code, database_url=f"sqlite:///{db_path}")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert not db_path.exists()


def test_deliberately_calling_create_app_does_initialize_the_database(tmp_path):
    """Contraste com os testes acima: chamar a factory DE PROPÓSITO deve,
    sim, inicializar tudo -- banco criado, tabelas migradas, sessão
    operacional criada. Prova que a correção não quebrou a inicialização
    real, só a removeu do caminho de importação."""
    db_path = tmp_path / "created_deliberately.db"
    assert not db_path.exists()

    code = (
        "from app.api.main import create_app\n"
        "app = create_app()\n"
        "print('OK')\n"
    )
    result = _run_subprocess(code, database_url=f"sqlite:///{db_path}")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout
    assert db_path.exists(), "create_app() chamado deliberadamente não inicializou o banco"

    import sqlite3
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    assert "system_state" in tables
    assert "operational_sessions" in tables
    cur.execute("SELECT COUNT(*) FROM operational_sessions")
    assert cur.fetchone()[0] == 1  # exatamente uma sessão criada, uma única vez
    con.close()


def test_launcher_uses_factory_mode_end_to_end(monkeypatch):
    """`python -m app.run` continua funcionando -- e constrói exatamente
    uma aplicação, através do modo factory do uvicorn, nunca de um `app`
    pré-construído."""
    import uvicorn

    import app.run as run_module
    from app.core.config import RunMode, Settings

    captured = {}

    def fake_uvicorn_run(app_path, **kwargs):
        captured["app_path"] = app_path
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setattr(
        run_module, "get_settings",
        lambda: Settings(mode=RunMode.REPLAY, api_host="127.0.0.1", api_port=9002),
    )

    run_module.run()

    assert captured["app_path"] == "app.api.main:create_app"
    assert captured.get("factory") is True
