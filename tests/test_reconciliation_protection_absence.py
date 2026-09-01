"""Correção Stop/Take v1.2 -- bloqueio reproduzido pelo auditor: o código só
comparava stop-loss/take-profit quando local E remoto eram simultaneamente
diferentes de `None`. Isso escondia exatamente o caso real -- BYBIT_DEMO
SEMPRE inclui as chaves `stop_loss`/`take_profit` no dicionário devolvido
por `get_position()`; um valor `None`/zero ali é a corretora dizendo
"sem proteção configurada", uma divergência real se o lado local tem um
nível -- mas `local.get(...)`/`remote.get(...)` tratava "chave ausente"
(PAPER, engine não suporta) e "chave presente com valor None" (BYBIT_DEMO,
proteção genuinamente ausente) como a mesma coisa.

A correção distingue pela PRESENÇA DA CHAVE, nunca pelo modo global.
"""
from __future__ import annotations

from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.execution.reconciliation import reconcile_positions
from app.persistence import repo
from app.persistence.db import session_scope
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_price_correctness import build_test_orchestrator

_BASE_LOCAL = {"symbol": "BTCUSDT", "side": "BUY", "qty": 1.0, "avg_entry_price": 100.0}
_BASE_REMOTE = {"side": "BUY", "qty": 1.0, "avg_entry_price": 100.0}


def _make_bybit_engine(transport) -> BybitDemoExecutionEngine:
    return BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get, sleep=lambda s: None,
    )


# --- Reprodução exata do bloqueio do auditor -------------------------------

def test_reproduction_local_protected_remote_reports_both_none_is_a_mismatch():
    """Cenário exato do auditor: local com stop=98/take=103, remoto com
    ambas as chaves presentes e `None` -- antes desta correção,
    `report.ok` era `True` e `mismatches` vazio (bug reproduzido); agora
    deve ser divergência."""
    local = [{**_BASE_LOCAL, "stop_loss": 98.0, "take_profit": 103.0}]
    remote = {"BTCUSDT": {**_BASE_REMOTE, "stop_loss": None, "take_profit": None}}

    report = reconcile_positions(local, remote)

    assert report.ok is False
    assert len(report.mismatches) == 2
    assert any("stop-loss" in m for m in report.mismatches)
    assert any("take-profit" in m for m in report.mismatches)


# --- As 5 regras exigidas, uma a uma ---------------------------------------

def test_only_stop_loss_remote_absent_is_a_mismatch():
    local = [{**_BASE_LOCAL, "stop_loss": 98.0, "take_profit": 103.0}]
    remote = {"BTCUSDT": {**_BASE_REMOTE, "stop_loss": None, "take_profit": 103.0}}
    report = reconcile_positions(local, remote)
    assert report.ok is False
    assert len(report.mismatches) == 1
    assert "stop-loss" in report.mismatches[0]


def test_only_take_profit_remote_absent_is_a_mismatch():
    local = [{**_BASE_LOCAL, "stop_loss": 98.0, "take_profit": 103.0}]
    remote = {"BTCUSDT": {**_BASE_REMOTE, "stop_loss": 98.0, "take_profit": None}}
    report = reconcile_positions(local, remote)
    assert report.ok is False
    assert len(report.mismatches) == 1
    assert "take-profit" in report.mismatches[0]


def test_remote_reports_protection_local_has_none_is_a_mismatch():
    """Regra 4: a corretora possui um nível que o estado local desconhece
    -- também é divergência, não só o caminho inverso."""
    local = [{**_BASE_LOCAL, "stop_loss": None, "take_profit": None}]
    remote = {"BTCUSDT": {**_BASE_REMOTE, "stop_loss": 98.0, "take_profit": 103.0}}
    report = reconcile_positions(local, remote)
    assert report.ok is False
    assert len(report.mismatches) == 2
    assert all("desconhecida localmente" in m for m in report.mismatches)


def test_paper_remote_without_the_keys_never_creates_a_false_mismatch():
    """Regra 1: PAPER_LOCAL/PAPER_LIVE nunca incluem `stop_loss`/
    `take_profit` no dicionário -- ausência ESTRUTURAL da chave nunca é
    tratada como divergência, mesmo com o local totalmente protegido."""
    local = [{**_BASE_LOCAL, "stop_loss": 98.0, "take_profit": 103.0}]
    remote = {"BTCUSDT": dict(_BASE_REMOTE)}  # sem as chaves -- engine não suporta
    report = reconcile_positions(local, remote)
    assert report.ok is True
    assert report.mismatches == []


def test_equal_levels_are_clean():
    local = [{**_BASE_LOCAL, "stop_loss": 98.0, "take_profit": 103.0}]
    remote = {"BTCUSDT": {**_BASE_REMOTE, "stop_loss": 98.0, "take_profit": 103.0}}
    report = reconcile_positions(local, remote)
    assert report.ok is True
    assert report.mismatches == []


def test_difference_within_tolerance_is_clean():
    local = [{**_BASE_LOCAL, "stop_loss": 98.00, "take_profit": 103.00}]
    remote = {"BTCUSDT": {**_BASE_REMOTE, "stop_loss": 98.005, "take_profit": 103.005}}  # << 0.1%
    report = reconcile_positions(local, remote)
    assert report.ok is True
    assert report.mismatches == []


def test_difference_above_tolerance_is_a_mismatch():
    local = [{**_BASE_LOCAL, "stop_loss": 98.0, "take_profit": 103.0}]
    remote = {"BTCUSDT": {**_BASE_REMOTE, "stop_loss": 95.0, "take_profit": 103.0}}  # > 0.1%
    report = reconcile_positions(local, remote)
    assert report.ok is False
    assert any("stop-loss" in m and "divergência de proteção" in m for m in report.mismatches)


def test_both_absent_key_present_none_local_none_is_clean():
    """Regra 5, variante com chave presente: se o engine reporta a chave
    mas ambos os lados concordam que não há proteção, não é divergência."""
    local = [{**_BASE_LOCAL, "stop_loss": None, "take_profit": None}]
    remote = {"BTCUSDT": {**_BASE_REMOTE, "stop_loss": None, "take_profit": None}}
    report = reconcile_positions(local, remote)
    assert report.ok is True
    assert report.mismatches == []


# --- Integração com Orchestrator.reconcile() -------------------------------

def test_orchestrator_reconcile_blocks_new_entries_on_protection_absence(session_factory):
    """A divergência detectada pela reconciliação de proteção deve
    propagar exatamente pelo caminho já existente: reconciliation_diverged,
    state_ambiguous, trading_blocked=True, mismatch estruturado persistido
    -- nenhum reparo automático."""
    transport = FakeBybitTransport()
    # A corretora reporta a posição SEM proteção (stop/take ausentes).
    transport.set_position("BTCUSDT", side="BUY", qty=0.01, avg_price=100.0)

    orch = build_test_orchestrator(session_factory, [])
    orch.execution_engine = _make_bybit_engine(transport)

    with session_scope(session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 100.0, stop_loss=90.0, take_profit=120.0)
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.reconciliation_diverged is True
        assert state.state_ambiguous is True
        assert state.trading_blocked is True
        assert "reconciliação" in (state.block_reason or "").lower()

        failures = repo.recent_failures(session, limit=10)
        matching = [f for f in failures if f.kind == "RECONCILIATION" and not f.resolved]
        assert matching
        assert any("proteção" in (f.detail or "").lower() for f in matching)

        events = repo.recent_security_events(session, limit=10)
        assert any(e.event_type == "RECONCILIATION_MISMATCH" for e in events)


def test_orchestrator_new_entries_blocked_but_closes_remain_allowed(session_factory):
    """Fechamentos/ações defensivas continuam permitidos -- só a checagem
    entry-only (RiskContext.reconciliation_stale/operational_state) é
    afetada; trading_blocked bloqueia tudo, mas evaluate_close() nunca
    consulta trading_blocked/reconciliation_diverged diretamente -- prova
    estrutural: evaluate_close() não referencia esses campos."""
    import inspect

    from app.risk.engine import RiskEngine

    source = inspect.getsource(RiskEngine.evaluate_close)
    assert "reconciliation_diverged" not in source
    assert "trading_blocked" not in source


def test_subsequent_clean_reconciliation_resolves_the_divergence(session_factory):
    """Nenhum reparo automático nesta detecção -- mas uma vez que a
    corretora volte a reportar os níveis corretos, uma reconciliação
    genuinamente limpa (mecanismo já existente) resolve normalmente."""
    transport = FakeBybitTransport()
    transport.set_position("BTCUSDT", side="BUY", qty=0.01, avg_price=100.0)  # sem proteção

    orch = build_test_orchestrator(session_factory, [])
    orch.execution_engine = _make_bybit_engine(transport)

    with session_scope(session_factory) as session:
        repo.open_position(session, "BTCUSDT", "BUY", 0.01, 100.0, stop_loss=90.0, take_profit=120.0)
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.trading_blocked is True

    # A corretora agora reporta os mesmos níveis locais.
    transport.set_position("BTCUSDT", side="BUY", qty=0.01, avg_price=100.0, stop_loss=90.0, take_profit=120.0)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch.reconcile(session, state)

    with session_scope(session_factory) as session:
        state = repo.get_or_create_system_state(session)
        assert state.reconciliation_diverged is False
        assert state.state_ambiguous is False
        assert state.trading_blocked is False
