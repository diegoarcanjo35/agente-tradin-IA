"""Correção Operacional do Poll Loop v1.1: provas dos três defeitos
reproduzidos pela auditoria independente sobre a v1.0.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.api import poll_engine
from app.api.poll_engine import (
    PollEngineStatus,
    PollHealth,
    engine_unhealthy,
    poll_worker,
    supervise_poll_loop,
    wait_for_in_flight_tick_before_shutdown,
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
        replay_done=False, poll_worker_task=None, poll_in_flight_future=None,
    ))
    return app


async def _run_briefly_and_cancel(coro_task, seconds: float) -> None:
    await asyncio.sleep(seconds)
    coro_task.cancel()
    try:
        await coro_task
    except asyncio.CancelledError:
        pass


# === Defeito 1: recuperação impossível após PARADO ==========================

@pytest.mark.asyncio
async def test_recovery_from_parado_after_heartbeat_expired_during_backoff(session_factory):
    """Reprodução exata do cenário da auditoria: falha única -> heartbeat
    vence DURANTE o backoff -> supervisor muda para PARADO -> N ticks
    saudáveis consecutivos -> deve voltar a SAUDAVEL, engine_degraded=False,
    exatamente UM evento de recuperação, e novas entradas voltam a ser
    elegíveis."""
    calls: list[int] = []

    def tick_fn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("falha única simulada")
        return {"status": "no_new_candle"}

    app = _make_app(
        tick_fn, session_factory,
        poll_backoff_initial_seconds=0.15, poll_backoff_max_seconds=0.15,
        poll_heartbeat_max_age_seconds=0.05, poll_healthy_ticks_to_recover=2,
        replay_poll_interval_seconds=0.01,
    )
    sup = asyncio.create_task(supervise_poll_loop(app))

    # Aguarda tempo suficiente para: falha (imediata) -> heartbeat vencer
    # durante o backoff de 0.15s (supervisor marca PARADO) -> backoff
    # terminar -> pelo menos 2 ticks saudáveis consecutivos depois.
    await asyncio.sleep(0.4)

    health = app.state.poll_health
    assert health.status == PollEngineStatus.SAUDAVEL
    assert app.state.orchestrator.engine_degraded is False
    assert health.poll_last_error is None
    assert engine_unhealthy(health, app.state.settings.poll_heartbeat_max_age_seconds) is False

    await _run_briefly_and_cancel(sup, 0.01)

    with session_scope(session_factory) as session:
        events = repo.recent_security_events(session, limit=50)
        recovered = [e for e in events if e.event_type == "POLL_LOOP_RECOVERED"]
        assert len(recovered) == 1  # exatamente uma recuperação, nunca mais


# === Defeito 2: INICIANDO/ENCERRANDO permitem ativação indevida =============

def test_activation_refused_while_iniciando():
    health = PollHealth()  # default: INICIANDO, nunca teve sucesso
    assert engine_unhealthy(health, max_heartbeat_age_seconds=60.0) is True


def test_activation_refused_while_encerrando():
    health = PollHealth(status=PollEngineStatus.ENCERRANDO)
    assert engine_unhealthy(health, max_heartbeat_age_seconds=60.0) is True


@pytest.mark.asyncio
async def test_status_stays_iniciando_during_the_first_slow_tick(session_factory):
    """Enquanto o PRIMEIRO tick ainda está em andamento (nunca terminou
    ainda), o status deve continuar INICIANDO -- nunca SAUDAVEL antes de
    qualquer prova real de sucesso."""
    release = threading.Event()

    def slow_first_tick():
        release.wait(timeout=2.0)
        return {"status": "no_new_candle"}

    app = _make_app(slow_first_tick, session_factory, poll_tick_timeout_seconds=5.0)
    worker = asyncio.create_task(poll_worker(app))
    await asyncio.sleep(0.1)

    assert app.state.poll_health.status == PollEngineStatus.INICIANDO
    assert engine_unhealthy(app.state.poll_health, 60.0) is True  # ativação recusada

    release.set()
    await _run_briefly_and_cancel(worker, 0.1)


@pytest.mark.asyncio
async def test_status_becomes_saudavel_only_after_one_genuine_success(session_factory):
    calls: list[int] = []

    def tick_fn():
        calls.append(1)
        return {"status": "no_new_candle"}

    app = _make_app(tick_fn, session_factory, replay_poll_interval_seconds=10.0)
    worker = asyncio.create_task(poll_worker(app))

    # Momento imediatamente após a criação -- ainda não houve tempo de
    # completar o primeiro tick de verdade (o event loop ainda não cedeu
    # controle o suficiente); ainda assim, status nunca deveria ser
    # SAUDAVEL até um sucesso genuíno ser registrado.
    await asyncio.sleep(0)
    if app.state.poll_health.status == PollEngineStatus.SAUDAVEL:
        assert app.state.poll_health.poll_last_success_at is not None

    await asyncio.sleep(0.05)
    assert app.state.poll_health.status == PollEngineStatus.SAUDAVEL
    assert app.state.poll_health.poll_last_success_at is not None
    assert len(calls) >= 1

    await _run_briefly_and_cancel(worker, 0.01)


def test_evaluate_close_still_never_checks_engine_degraded():
    from app.core.clock import utcnow
    from app.risk.config import RiskLimits
    from app.risk.engine import RiskContext, RiskEngine

    engine = RiskEngine(RiskLimits())
    context = RiskContext(
        open_positions_count=1, open_exposure_usd=10.0, daily_realized_loss_usd=0.0,
        consecutive_losses=0, data_is_stale=False, api_failure_count=0,
        clock_drift_seconds=0.0, kill_switch_engaged=False, trading_blocked=False,
        state_ambiguous=False, cooldown_until=None, now=utcnow(),
        engine_degraded=True,
    )
    result = engine.evaluate_close(
        signal_id=1, symbol="BTCUSDT", close_side="SELL", qty=0.01,
        position_exists=True, position_qty=0.01, position_side="BUY",
        context=context,
    )
    assert result.approved is True


# === Defeito 3: tick continua alterando estado depois do shutdown ===========

@pytest.mark.asyncio
async def test_stuck_tick_is_never_abandoned_never_resubmitted_across_timeouts(session_factory):
    """Um tick que excede o limite lógico repetidamente NUNCA é reenviado
    -- a mesma chamada síncrona de `orch.tick()` só acontece uma única vez,
    mesmo com vários ciclos de timeout esperando por ela."""
    call_count = {"n": 0}
    release = threading.Event()

    def stuck_tick():
        call_count["n"] += 1
        release.wait(timeout=5.0)
        return {"status": "no_new_candle"}

    app = _make_app(
        stuck_tick, session_factory,
        poll_tick_timeout_seconds=0.05, poll_backoff_initial_seconds=0.02, poll_backoff_max_seconds=0.03,
    )
    worker = asyncio.create_task(poll_worker(app))
    await asyncio.sleep(0.3)  # várias janelas de timeout já deveriam ter passado

    assert call_count["n"] == 1  # nunca reenviado -- ainda a mesma chamada original
    assert app.state.poll_health.status == PollEngineStatus.DEGRADADO
    assert app.state.poll_in_flight_future is not None
    assert not app.state.poll_in_flight_future.done()

    release.set()
    await asyncio.sleep(0.2)
    # Só DEPOIS que a chamada original terminou o polling normal retoma e
    # gera chamadas novas de verdade -- retomada real, não travamento.
    assert call_count["n"] > 1
    assert app.state.poll_health.status == PollEngineStatus.SAUDAVEL  # recupera de verdade

    await _run_briefly_and_cancel(worker, 0.01)

    with session_scope(session_factory) as session:
        events = [e for e in repo.recent_security_events(session, limit=50) if e.event_type == "POLL_LOOP_TICK_FAILED"]
        # Um único evento para o episódio inteiro do tick pendurado -- não
        # um por re-checagem de timeout (sem tempestade de logs).
        assert len(events) == 1


@pytest.mark.asyncio
async def test_no_database_write_happens_after_shutdown_reconciliation_starts(session_factory):
    """Prova central do defeito 3: uma escrita tardia do tick (que só
    termina DEPOIS do timeout ter sido detectado) nunca pode acontecer
    depois que a reconciliação final do shutdown já começou -- a espera é
    real, não apenas um `sleep` cosmético."""
    order_of_events: list[str] = []
    release = threading.Event()

    def late_writing_tick():
        release.wait(timeout=5.0)
        order_of_events.append("tick_wrote_to_db")
        return {"status": "no_new_candle"}

    app = _make_app(
        late_writing_tick, session_factory,
        poll_tick_timeout_seconds=0.05, poll_backoff_initial_seconds=0.02,
    )
    worker = asyncio.create_task(poll_worker(app))
    await asyncio.sleep(0.15)  # o timeout já disparou; o tick ainda está "vivo"
    assert app.state.poll_health.status == PollEngineStatus.DEGRADADO

    async def simulated_shutdown_sequence():
        await wait_for_in_flight_tick_before_shutdown(app)
        order_of_events.append("shutdown_reconciliation_started")

    shutdown_task = asyncio.create_task(simulated_shutdown_sequence())
    await asyncio.sleep(0.1)
    assert order_of_events == []  # shutdown ainda aguardando -- nada aconteceu ainda

    release.set()  # o tick finalmente termina
    await shutdown_task
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    # A escrita do tick SEMPRE precede o início da reconciliação de
    # encerramento -- nunca o contrário.
    assert order_of_events == ["tick_wrote_to_db", "shutdown_reconciliation_started"]

    with session_scope(session_factory) as session:
        events = [
            e for e in repo.recent_security_events(session, limit=50)
            if e.event_type == "POLL_LOOP_SHUTDOWN_TICK_PENDING"
        ]
        assert len(events) == 1  # aviso explícito e único registrado


@pytest.mark.asyncio
async def test_shutdown_wait_returns_immediately_when_no_tick_is_in_flight(session_factory):
    app = _make_app(lambda: {"status": "no_new_candle"}, session_factory)
    app.state.poll_in_flight_future = None
    start = time.monotonic()
    await wait_for_in_flight_tick_before_shutdown(app)
    assert time.monotonic() - start < 0.05


@pytest.mark.asyncio
async def test_panel_stays_responsive_while_shutdown_waits_for_a_stuck_tick(session_factory):
    release = threading.Event()

    def stuck_tick():
        release.wait(timeout=5.0)
        return {"status": "no_new_candle"}

    app = _make_app(stuck_tick, session_factory, poll_tick_timeout_seconds=0.05)
    worker = asyncio.create_task(poll_worker(app))
    await asyncio.sleep(0.15)

    shutdown_task = asyncio.create_task(wait_for_in_flight_tick_before_shutdown(app))

    ping_times = []

    async def fake_panel_ping():
        for _ in range(5):
            start = time.monotonic()
            await asyncio.sleep(0.01)
            ping_times.append(time.monotonic() - start)

    await fake_panel_ping()
    assert all(t < 0.08 for t in ping_times)  # o painel nunca ficou preso

    release.set()
    await shutdown_task
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_resumes_normal_polling_after_the_stuck_tick_genuinely_finishes(session_factory):
    """"Provar retomada após a chamada anterior realmente terminar" --
    depois que o tick pendurado finalmente resolve, o worker volta a
    operar normalmente (novos ticks acontecem no intervalo configurado)."""
    call_count = {"n": 0}
    release = threading.Event()

    def first_stuck_then_normal():
        call_count["n"] += 1
        if call_count["n"] == 1:
            release.wait(timeout=5.0)
        return {"status": "no_new_candle"}

    app = _make_app(
        first_stuck_then_normal, session_factory,
        poll_tick_timeout_seconds=0.05, poll_backoff_initial_seconds=0.02,
        replay_poll_interval_seconds=0.02,
    )
    worker = asyncio.create_task(poll_worker(app))
    await asyncio.sleep(0.12)
    assert call_count["n"] == 1  # ainda preso no primeiro

    release.set()
    await asyncio.sleep(0.15)
    assert call_count["n"] >= 3  # retomou o polling normal -- vários ticks novos aconteceram

    await _run_briefly_and_cancel(worker, 0.01)


# === Validação complementar (fail-fast) ======================================

@pytest.mark.parametrize("field", [
    "poll_tick_timeout_seconds", "poll_backoff_initial_seconds",
    "poll_backoff_max_seconds", "poll_heartbeat_max_age_seconds", "bybit_http_timeout_seconds",
])
def test_non_positive_poll_durations_are_rejected_at_construction(field):
    with pytest.raises(Exception):
        Settings(**{field: 0.0}, database_url="sqlite:///:memory:")
    with pytest.raises(Exception):
        Settings(**{field: -1.0}, database_url="sqlite:///:memory:")


def test_poll_healthy_ticks_to_recover_below_one_is_rejected():
    with pytest.raises(Exception):
        Settings(poll_healthy_ticks_to_recover=0, database_url="sqlite:///:memory:")


def test_backoff_max_below_initial_is_rejected():
    with pytest.raises(Exception):
        Settings(
            poll_backoff_initial_seconds=10.0, poll_backoff_max_seconds=5.0,
            database_url="sqlite:///:memory:",
        )


def test_backoff_max_equal_to_initial_is_accepted():
    settings = Settings(
        poll_backoff_initial_seconds=5.0, poll_backoff_max_seconds=5.0,
        database_url="sqlite:///:memory:",
    )
    assert settings.poll_backoff_max_seconds == settings.poll_backoff_initial_seconds


def test_sub_one_second_pybit_timeout_is_never_truncated_to_zero():
    from app.execution.bybit_pybit_client import _pybit_timeout

    assert _pybit_timeout(0.4) >= 1
    assert _pybit_timeout(0.01) >= 1
