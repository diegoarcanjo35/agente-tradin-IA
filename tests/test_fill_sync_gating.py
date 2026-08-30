"""Correção da Fase 2 v1.2 #1/#2: an order must NEVER be terminalized
(FILLED/CANCELLED persisted as `order.status`) before its fill history is
comprovadamente completo. The audited defect: `poll_order()` could report
`status=FILLED, fills=[]` after `/v5/execution/list` timed out or rate-
limited, `fill_service.apply_order_snapshot()` would persist FILLED anyway,
and `repo.non_terminal_orders()` would then drop the order from the
recoverable set forever -- permanently losing its fills/position/fees.

Diagrama textual dos estados separados:

    status da ordem (Order.status)      sincronização de fills
    ------------------------------      -----------------------
    SUBMITTED (inalterado)         <->  fills_sync_status = "PENDING"
                                         pending_exchange_status = "FILLED"
           |
           | poll_order() com fills_complete=True
           v
    FILLED (transição real)        <->  fills_sync_status = "COMPLETE"
                                         pending_exchange_status = None

    A ordem só é retirada do conjunto recuperável (repo.non_terminal_orders)
    quando `Order.status` de fato se torna terminal -- o que só acontece na
    transição real, nunca enquanto fills_sync_status="PENDING".
"""
from __future__ import annotations

import pytest

from app.api.main import build_orchestrator
from app.core.errors import ExchangeTimeoutError, RateLimitError
from app.execution.order_state import OrderStatus
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Order
from tests.fakes.bybit_fake import FakeBybitTransport
from tests.test_bybit_demo_wiring import make_bybit_demo_settings


def _make_submitted_order(orch, exchange_order_id: str, idempotency_key: str) -> int:
    with session_scope(orch.session_factory) as session:
        signal = repo.save_signal(session, "BTCUSDT", "BUY", "teste", 100.0, 1.0, {})
        risk_eval = repo.save_risk_evaluation(session, signal.id, True, "aprovado", {})
        order = repo.save_order(
            session, idempotency_key=idempotency_key, risk_evaluation_id=risk_eval.id,
            symbol="BTCUSDT", side="BUY", qty=0.01, stop_loss=90.0, take_profit=110.0, mode="BYBIT_DEMO",
        )
        repo.transition_order_status(session, order, OrderStatus.SUBMITTED)
        order.exchange_order_id = exchange_order_id
        return order.id


def _poll_once(orch):
    with session_scope(orch.session_factory) as session:
        state = repo.get_or_create_system_state(session)
        orch._last_open_order_poll_at = None
        orch._maybe_poll_open_orders(session, state)


def test_filled_status_with_execution_list_timeout_keeps_order_recoverable(tmp_path):
    """Reprodução exata do defeito auditado: /v5/order/realtime informa
    Filled, /v5/execution/list sofre timeout -- a ordem NUNCA pode virar
    terminal, e deve continuar no conjunto recuperável."""
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'sync1.db'}")
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    order_id = _make_submitted_order(orch, "EX-SYNC-1", "sync-1")

    transport.queue_status("EX-SYNC-1", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-SYNC-1", [ExchangeTimeoutError("timeout simulado")])

    _poll_once(orch)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTED.value  # NUNCA terminalizada
        assert order.fills_sync_status == "PENDING"
        assert order.pending_exchange_status == OrderStatus.FILLED.value
        assert order.filled_qty == 0.0  # nada fabricado

        non_terminal_ids = {o.id for o in repo.non_terminal_orders(session, mode="BYBIT_DEMO")}
        assert order_id in non_terminal_ids


def test_next_poll_with_full_history_applies_fills_then_terminalizes_atomically(tmp_path):
    """No poll seguinte, com o histórico disponível: todos os fills são
    gravados, posição/taxas aplicadas, e SOMENTE ENTÃO a ordem termina."""
    settings = make_bybit_demo_settings(
        risk_max_position_usd=1000.0, risk_max_total_exposure_usd=1000.0,
        database_url=f"sqlite:///{tmp_path / 'sync2.db'}",
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    order_id = _make_submitted_order(orch, "EX-SYNC-2", "sync-2")

    transport.queue_status("EX-SYNC-2", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-SYNC-2", [ExchangeTimeoutError("timeout simulado")])
    _poll_once(orch)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTED.value

    # Segundo poll: histórico completo desta vez.
    transport.queue_status("EX-SYNC-2", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-SYNC-2", [
        {"list": [{"execId": "EXEC-SYNC-2", "execQty": "0.01", "execPrice": "100.0", "execFee": "0.001"}]},
    ])
    _poll_once(orch)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value
        assert order.fills_sync_status == "COMPLETE"
        assert order.pending_exchange_status is None
        assert order.filled_qty == pytest.approx(0.01)
        assert order.fees_total == pytest.approx(0.001)

        positions = repo.open_positions(session, "BTCUSDT")
        assert len(positions) == 1
        assert positions[0].qty == pytest.approx(0.01)

        non_terminal_ids = {o.id for o in repo.non_terminal_orders(session, mode="BYBIT_DEMO")}
        assert order_id not in non_terminal_ids  # agora sim, sai do conjunto recuperável


def test_restart_between_the_two_polls_still_resumes_from_the_database(tmp_path):
    """Reinício entre os dois polls: uma instância nova de Orchestrator,
    sem nada compartilhado em memória, retoma via o banco -- sem reenviar
    a ordem."""
    settings = make_bybit_demo_settings(database_url=f"sqlite:///{tmp_path / 'sync3.db'}")
    transport = FakeBybitTransport()
    orch_before = build_orchestrator(settings, bybit_transport=transport)
    order_id = _make_submitted_order(orch_before, "EX-SYNC-3", "sync-3")

    transport.queue_status("EX-SYNC-3", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-SYNC-3", [ExchangeTimeoutError("timeout simulado")])
    _poll_once(orch_before)

    with session_scope(orch_before.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.fills_sync_status == "PENDING"

    # "Reinício": nova instância de Orchestrator (motor de execução novo
    # também), mesma base de dados e mesmo transporte fake (simulando a
    # mesma corretora real).
    orch_after = build_orchestrator(settings, bybit_transport=transport)
    transport.queue_status("EX-SYNC-3", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-SYNC-3", [
        {"list": [{"execId": "EXEC-SYNC-3", "execQty": "0.01", "execPrice": "100.0", "execFee": "0.001"}]},
    ])
    _poll_once(orch_after)

    with session_scope(orch_after.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value
        assert order.fills_sync_status == "COMPLETE"

    create_calls = [c for c in transport.post_calls if c[0].endswith("/v5/order/create")]
    assert len(create_calls) == 0  # nunca reenviada


def test_cancellation_with_residual_fill_also_defers_until_fills_sync_complete(tmp_path):
    """Mesmo cenário para PartiallyFilledCanceled/cancelamento com fill
    residual -- CANCELLED também é um status terminal, então também precisa
    da sincronização de fills antes de ser persistido."""
    settings = make_bybit_demo_settings(
        risk_max_position_usd=1000.0, risk_max_total_exposure_usd=1000.0,
        database_url=f"sqlite:///{tmp_path / 'sync4.db'}",
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    order_id = _make_submitted_order(orch, "EX-SYNC-4", "sync-4")

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        repo.transition_order_status(session, order, OrderStatus.CANCEL_PENDING)

    transport.queue_status("EX-SYNC-4", [{"orderStatus": "Cancelled"}])
    transport.queue_execution_pages("EX-SYNC-4", [RateLimitError("rate limit simulado")])
    _poll_once(orch)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.CANCEL_PENDING.value  # nunca CANCELLED ainda
        assert order.fills_sync_status == "PENDING"
        assert order.pending_exchange_status == OrderStatus.CANCELLED.value

    # Poll seguinte: histórico completo revela um fill residual (a ordem
    # venceu parcialmente a corrida antes do cancelamento ser processado).
    transport.queue_status("EX-SYNC-4", [{"orderStatus": "Cancelled"}])
    transport.queue_execution_pages("EX-SYNC-4", [
        {"list": [{"execId": "EXEC-SYNC-4", "execQty": "0.005", "execPrice": "100.0", "execFee": "0.0005"}]},
    ])
    _poll_once(orch)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.CANCELLED.value
        assert order.filled_qty == pytest.approx(0.005)  # fill residual aplicado, não perdido
        positions = repo.open_positions(session, "BTCUSDT")
        assert len(positions) == 1
        assert positions[0].qty == pytest.approx(0.005)


def test_failure_on_a_later_page_still_records_the_earlier_pages_fills_without_duplication(tmp_path):
    """Falha na segunda página do histórico: progresso seguro, sem perda
    nem duplicação -- os fills da primeira página são gravados mesmo com a
    ordem permanecendo pendente, e o poll seguinte não os duplica."""
    settings = make_bybit_demo_settings(
        risk_max_position_usd=1000.0, risk_max_total_exposure_usd=1000.0,
        database_url=f"sqlite:///{tmp_path / 'sync5.db'}",
    )
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)
    order_id = _make_submitted_order(orch, "EX-SYNC-5", "sync-5")

    transport.queue_status("EX-SYNC-5", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-SYNC-5", [
        {"list": [{"execId": "EXEC-P1", "execQty": "0.004", "execPrice": "100.0", "execFee": "0.0004"}],
         "nextPageCursor": "cursor-1"},
        ExchangeTimeoutError("timeout na segunda página"),
    ])
    _poll_once(orch)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.SUBMITTED.value  # ainda pendente
        assert order.fills_sync_status == "PENDING"
        assert order.filled_qty == pytest.approx(0.004)  # progresso da 1ª página preservado

    # Poll seguinte: histórico completo (as duas páginas, incluindo a
    # primeira já registrada) -- não deve duplicar EXEC-P1.
    transport.queue_status("EX-SYNC-5", [{"orderStatus": "Filled"}])
    transport.queue_execution_pages("EX-SYNC-5", [
        {"list": [{"execId": "EXEC-P1", "execQty": "0.004", "execPrice": "100.0", "execFee": "0.0004"}],
         "nextPageCursor": "cursor-1"},
        {"list": [{"execId": "EXEC-P2", "execQty": "0.006", "execPrice": "100.2", "execFee": "0.0006"}]},
    ])
    _poll_once(orch)

    with session_scope(orch.session_factory) as session:
        order = session.get(Order, order_id)
        assert order.status == OrderStatus.FILLED.value
        assert order.filled_qty == pytest.approx(0.01)  # 0.004 + 0.006, nunca 0.014 nem duplicado
        assert order.fees_total == pytest.approx(0.001)
