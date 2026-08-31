"""Correção Operacional do Poll Loop v1.0: testes adversariais do novo
supervisor/worker (app/api/poll_engine.py).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.api import poll_engine
from app.api.poll_engine import (
    PollEngineStatus,
    PollHealth,
    is_heartbeat_expired,
    poll_worker,
    supervise_poll_loop,
)
from app.core.config import RunMode, Settings
from app.persistence import repo
from app.persistence.db import session_scope


def _make_app(tick_fn, session_factory, **settings_overrides):
    defaults = dict(
        mode=RunMode.REPLAY,
        database_url="sqlite:///:memory:",
        replay_poll_interval_seconds=0.01,
        poll_tick_timeout_seconds=0.2,
        poll_backoff_initial_seconds=0.02,
        poll_backoff_max_seconds=0.06,
        poll_healthy_ticks_to_recover=2,
        poll_heartbeat_max_age_seconds=0.3,
    )
    defaults.update(settings_overrides)
    settings = Settings(**defaults)

    orch = SimpleNamespace(tick=tick_fn, engine_degraded=False, session_factory=session_factory)
    app = SimpleNamespace(state=SimpleNamespace(
        orchestrator=orch, settings=settings, poll_health=PollHealth(),
        replay_done=False, poll_worker_task=None,
    ))
    return app


async def _run_briefly_and_cancel(coro_task, seconds: float) -> None:
    await asyncio.sleep(seconds)
    coro_task.cancel()
    try:
        await coro_task
    except asyncio.CancelledError:
        pass


# --- 1: uma exceção inesperada é barreirada e o motor retoma -------------

@pytest.mark.asyncio
async def test_single_unexpected_exception_is_barriered_and_engine_recovers(session_factory):
    calls: list[int] = []

    def tick_fn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("falha simulada")
        return {"status": "no_new_candle"}

    app = _make_app(tick_fn, session_factory, poll_healthy_ticks_to_recover=2)
    worker = asyncio.create_task(poll_worker(app))
    await _run_briefly_and_cancel(worker, 0.3)

    assert len(calls) >= 4  # a 1ª falhou, seguida de várias saudáveis
    assert app.state.poll_health.status == PollEngineStatus.SAUDAVEL
    assert app.state.orchestrator.engine_degraded is False

    with session_scope(session_factory) as session:
        failures = repo.recent_failures(session, limit=20)
        assert any("inesperada" in f.detail.lower() for f in failures)
        events = repo.recent_security_events(session, limit=20)
        assert any(e.event_type == "POLL_LOOP_TICK_FAILED" for e in events)
        assert any(e.event_type == "POLL_LOOP_RECOVERED" for e in events)


# --- 2: várias falhas seguidas -- backoff, contagem correta --------------

@pytest.mark.asyncio
async def test_consecutive_failures_apply_backoff_and_count_correctly(session_factory):
    calls: list[int] = []

    def tick_fn():
        calls.append(1)
        raise RuntimeError("falha persistente")

    app = _make_app(tick_fn, session_factory, poll_backoff_initial_seconds=0.02, poll_backoff_max_seconds=0.04)
    worker = asyncio.create_task(poll_worker(app))
    await _run_briefly_and_cancel(worker, 0.15)

    health = app.state.poll_health
    assert health.status == PollEngineStatus.DEGRADADO
    assert health.poll_consecutive_failures >= 2
    assert app.state.orchestrator.engine_degraded is True

    with session_scope(session_factory) as session:
        events = [e for e in repo.recent_security_events(session, limit=50) if e.event_type == "POLL_LOOP_TICK_FAILED"]
        # Um evento por tentativa de tick -- nem mais, nem menos (sem
        # tempestade nem perda).
        assert len(events) == len(calls)


# --- 3: término inesperado da task fora da barreira interna --------------

@pytest.mark.asyncio
async def test_supervisor_detects_and_restarts_a_dead_worker_exactly_once(monkeypatch, session_factory):
    attempts = {"n": 0}

    async def fake_worker(app):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("worker morreu inesperadamente (fora da barreira interna)")
        await asyncio.sleep(10)  # 2ª tentativa: fica "viva" até ser cancelada

    monkeypatch.setattr(poll_engine, "poll_worker", fake_worker)
    app = _make_app(lambda: {"status": "no_new_candle"}, session_factory, poll_backoff_initial_seconds=0.02)
    sup = asyncio.create_task(supervise_poll_loop(app))
    await asyncio.sleep(0.15)

    assert attempts["n"] == 2  # reiniciado exatamente uma vez
    assert app.state.poll_health.restart_count == 1
    assert app.state.poll_health.status == PollEngineStatus.PARADO

    await _run_briefly_and_cancel(sup, 0.01)

    with session_scope(session_factory) as session:
        events = repo.recent_security_events(session, limit=20)
        assert any(e.event_type == "POLL_LOOP_TASK_DIED" for e in events)


@pytest.mark.asyncio
async def test_supervisor_never_runs_two_worker_tasks_at_once(monkeypatch, session_factory):
    concurrent_count = {"current": 0, "max": 0}

    async def fake_worker(app):
        concurrent_count["current"] += 1
        concurrent_count["max"] = max(concurrent_count["max"], concurrent_count["current"])
        try:
            raise RuntimeError("falha simulada")
        finally:
            concurrent_count["current"] -= 1

    monkeypatch.setattr(poll_engine, "poll_worker", fake_worker)
    app = _make_app(lambda: {"status": "no_new_candle"}, session_factory, poll_backoff_initial_seconds=0.01)
    sup = asyncio.create_task(supervise_poll_loop(app))
    await _run_briefly_and_cancel(sup, 0.1)

    assert concurrent_count["max"] == 1  # nunca duas tasks de worker simultâneas


# --- 4: heartbeat vencido com HTTP saudável -------------------------------

def test_heartbeat_expiry_detection_is_pure_and_correct():
    health = PollHealth()
    assert is_heartbeat_expired(health, max_age_seconds=60.0) is False  # ainda não iniciou -- não é "vencido"

    from app.core.clock import utcnow
    health.poll_last_success_at = utcnow()
    assert is_heartbeat_expired(health, max_age_seconds=60.0) is False

    from datetime import timedelta
    health.poll_last_success_at = utcnow() - timedelta(seconds=120)
    assert is_heartbeat_expired(health, max_age_seconds=60.0) is True


@pytest.mark.asyncio
async def test_supervisor_marks_parado_and_alerts_once_when_heartbeat_expires(session_factory):
    """Motor tecnicamente "vivo" (worker nunca termina) mas nunca tem
    sucesso -- o heartbeat vence e o supervisor detecta isso
    independentemente da barreira interna do worker."""
    async def stuck_worker(app):
        # nunca atualiza poll_last_success_at -- simula um worker
        # tecnicamente rodando mas travado num jeito que a barreira
        # interna não capta.
        await asyncio.sleep(10)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(poll_engine, "poll_worker", stuck_worker)
        app = _make_app(
            lambda: {"status": "no_new_candle"}, session_factory,
            poll_heartbeat_max_age_seconds=0.05,
        )
        sup = asyncio.create_task(supervise_poll_loop(app))
        await asyncio.sleep(0.25)

        health = app.state.poll_health
        assert health.status == PollEngineStatus.PARADO
        assert app.state.orchestrator.engine_degraded is True
        assert health.heartbeat_alert_active is True

        await _run_briefly_and_cancel(sup, 0.01)

        with session_scope(session_factory) as session:
            events = [e for e in repo.recent_security_events(session, limit=50) if e.event_type == "POLL_LOOP_HEARTBEAT_EXPIRED"]
            # Alertado uma única vez por episódio, mesmo com várias checagens
            # de heartbeat acontecendo (0.25s / intervalo de checagem curto).
            assert len(events) == 1


# --- 5: chamada de mercado lenta -- painel continua respondendo ----------

@pytest.mark.asyncio
async def test_panel_stays_responsive_during_a_slow_market_call(session_factory):
    def slow_tick_fn():
        time.sleep(0.3)  # bloqueante, síncrono -- simula uma chamada de mercado lenta
        return {"status": "no_new_candle"}

    app = _make_app(slow_tick_fn, session_factory, poll_tick_timeout_seconds=5.0, replay_poll_interval_seconds=0.01)
    worker = asyncio.create_task(poll_worker(app))

    # Enquanto o tick síncrono lento roda numa thread separada, o event
    # loop principal (onde o "painel" -- uma tarefa async barata -- viveria)
    # continua livre para responder.
    ping_times = []

    async def fake_panel_ping():
        for _ in range(5):
            start = time.monotonic()
            await asyncio.sleep(0.01)
            ping_times.append(time.monotonic() - start)

    await fake_panel_ping()
    await _run_briefly_and_cancel(worker, 0.05)

    # Cada "ping" do painel respondeu dentro de uma folga pequena --
    # nunca ficou preso atrás dos 0.3s do tick lento.
    assert all(t < 0.1 for t in ping_times)


# --- 6: timeout de mercado -- erro tipado, sem sobreposição, recuperação -

@pytest.mark.asyncio
async def test_market_call_timeout_is_typed_never_overlaps_and_recovers(session_factory):
    concurrent = {"current": 0, "max": 0}
    call_count = {"n": 0}

    def tick_fn():
        call_count["n"] += 1
        concurrent["current"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["current"])
        try:
            if call_count["n"] == 1:
                time.sleep(0.3)  # excede o timeout configurado abaixo
            return {"status": "no_new_candle"}
        finally:
            concurrent["current"] -= 1

    app = _make_app(
        tick_fn, session_factory,
        poll_tick_timeout_seconds=0.05, poll_backoff_initial_seconds=0.02,
        poll_healthy_ticks_to_recover=1,
    )
    worker = asyncio.create_task(poll_worker(app))
    await _run_briefly_and_cancel(worker, 0.6)

    assert "TimeoutError" in (app.state.poll_health.poll_last_error or "") or app.state.poll_health.status == PollEngineStatus.SAUDAVEL
    assert concurrent["max"] == 1  # nunca duas execuções de tick concorrentes

    with session_scope(session_factory) as session:
        events = repo.recent_security_events(session, limit=50)
        assert any(e.event_type == "POLL_LOOP_TICK_FAILED" and "tempo limite" in e.detail.lower() for e in events)


# --- 7: reinício -- heartbeat é só de processo, incidentes persistem -----

def test_poll_health_is_process_memory_only_but_incidents_persist(session_factory):
    with session_scope(session_factory) as session:
        repo.record_failure(session, "FAILURE", "Falha inesperada no ciclo de mercado (motor de mercado).")
        repo.record_security_event(session, "POLL_LOOP_TICK_FAILED", "incidente antes do reinício simulado")

    # "Reinício": uma nova instância de PollHealth, sem nenhum estado do
    # processo anterior -- exatamente o comportamento documentado e
    # esperado (nunca finge que sobreviveu).
    fresh_health = PollHealth()
    assert fresh_health.status == PollEngineStatus.INICIANDO
    assert fresh_health.poll_consecutive_failures == 0

    # Mas o INCIDENTE em si continua no banco, disponível para auditoria.
    with session_scope(session_factory) as session:
        events = repo.recent_security_events(session, limit=20)
        assert any(e.event_type == "POLL_LOOP_TICK_FAILED" for e in events)


# --- 8: shutdown gracioso -- nenhuma task é recriada durante encerramento -

@pytest.mark.asyncio
async def test_graceful_shutdown_never_recreates_a_worker_task(monkeypatch, session_factory):
    attempts = {"n": 0}

    async def fake_worker(app):
        attempts["n"] += 1
        await asyncio.sleep(10)

    monkeypatch.setattr(poll_engine, "poll_worker", fake_worker)
    app = _make_app(lambda: {"status": "no_new_candle"}, session_factory)
    sup = asyncio.create_task(supervise_poll_loop(app))
    await asyncio.sleep(0.05)
    assert attempts["n"] == 1

    sup.cancel()
    try:
        await sup
    except asyncio.CancelledError:
        pass

    await asyncio.sleep(0.05)
    assert attempts["n"] == 1  # nenhuma nova task criada após o cancelamento
    assert app.state.poll_health.status == PollEngineStatus.ENCERRANDO


# --- 9: seleção de intervalo por modo continua correta --------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected_field", [
    (RunMode.REPLAY, "replay_poll_interval_seconds"),
    (RunMode.PAPER_LOCAL, "replay_poll_interval_seconds"),
    (RunMode.PAPER_LIVE, "bybit_poll_interval_seconds"),
    (RunMode.BYBIT_DEMO, "bybit_poll_interval_seconds"),
])
async def test_poll_interval_selection_preserved_per_mode(mode, expected_field, session_factory, tmp_path):
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def spy_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()
        await real_sleep(0.001)

    kwargs = dict(
        mode=mode, database_url=f"sqlite:///{tmp_path / 'interval_mode.db'}",
        bybit_poll_interval_seconds=7.0, replay_poll_interval_seconds=0.03,
    )
    if mode in (RunMode.BYBIT_DEMO,):
        kwargs.update(bybit_api_key="k", bybit_api_secret="s")
    app = _make_app(lambda: {"status": "no_new_candle"}, session_factory, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "sleep", spy_sleep)
        with pytest.raises(asyncio.CancelledError):
            await poll_worker(app)

    expected = getattr(app.state.settings, expected_field)
    assert expected in sleep_calls
