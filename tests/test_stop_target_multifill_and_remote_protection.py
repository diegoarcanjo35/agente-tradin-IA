"""Complementação da Correção Stop/Take Pós-Preenchimento v1.1.

Bloqueio 1: múltiplos fills da mesma ordem (mesmo snapshot ou snapshots
diferentes) voltavam a ancorar stop/alvo no preço médio anterior --
`repo.add_to_position()` recalcula `avg_entry_price` mas nada reposicionava
`position.stop_loss`/`take_profit` depois do primeiro fill.

Bloqueio 2: a proteção remota (BYBIT_DEMO, `stopLoss`/`takeProfit` enviados
na criação) nunca era sincronizada de novo depois do preenchimento -- a
posição local podia ficar com níveis corretos (derivados do fill) enquanto
a posição na corretora continuava com os níveis antigos (derivados do
sinal).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.execution import fill_service
from app.execution.base import FillEvent, OrderStatusSnapshot
from app.execution.bybit_demo import BybitDemoExecutionEngine
from app.execution.order_state import OrderStatus
from app.execution.paper_local import PaperLocalExecutionEngine
from app.execution.reconciliation import reconcile_positions
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Order, OperationalSession, Position, RiskEvaluation, StrategySignal, SystemState
from tests.fakes.bybit_fake import FakeBybitTransport

STOP_MULT = 2.0
TAKE_MULT = 3.0


def _make_bybit_engine(transport) -> BybitDemoExecutionEngine:
    return BybitDemoExecutionEngine(
        "https://api-demo.bybit.com", transport.http_post, transport.http_get, sleep=lambda s: None,
    )


def _persist_entry_order(session, *, side, qty, reference_price, atr, symbol="BTCUSDT", idem="order-1"):
    signal = StrategySignal(
        symbol=symbol, direction=side, justification="teste", observed_price=reference_price,
        atr=atr, params_json="{}",
    )
    session.add(signal)
    session.flush()
    risk_eval = RiskEvaluation(signal_id=signal.id, approved=True, reason="teste", checks_json="{}")
    session.add(risk_eval)
    session.flush()
    if side == "BUY":
        stop_loss = reference_price - STOP_MULT * atr
        take_profit = reference_price + TAKE_MULT * atr
    else:
        stop_loss = reference_price + STOP_MULT * atr
        take_profit = reference_price - TAKE_MULT * atr
    order = Order(
        idempotency_key=idem, risk_evaluation_id=risk_eval.id, symbol=symbol, side=side, qty=qty,
        stop_loss=stop_loss, take_profit=take_profit, is_close=False, status=OrderStatus.SUBMITTED.value,
        mode="BYBIT_DEMO", reference_price=reference_price,
    )
    session.add(order)
    session.flush()
    return order.id, signal.atr


def _fresh_state(session) -> SystemState:
    state = SystemState()
    session.add(state)
    session.flush()
    return state


def _apply(session, state, order_id, fills, execution_engine, is_close=False):
    order = session.get(Order, order_id)
    snapshot = OrderStatusSnapshot(exchange_order_id="X", status=OrderStatus.FILLED, fills=fills)
    return fill_service.apply_order_snapshot(
        session, state, None, order, snapshot, is_close=is_close, max_api_failures=5,
        execution_engine=execution_engine,
    )


# --- Bloqueio 1: múltiplos fills ------------------------------------------

@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_two_fills_same_snapshot_recompute_from_weighted_average(session_factory, side):
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 0.0, slippage_bps=0.0)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, atr = _persist_entry_order(session, side=side, qty=0.002, reference_price=100.0, atr=10.0)

        price_a, price_b = (101.0, 103.0) if side == "BUY" else (99.0, 97.0)
        fills = [
            FillEvent(exchange_fill_id="f1", fill_qty=0.001, fill_price=price_a, fee=0.01),
            FillEvent(exchange_fill_id="f2", fill_qty=0.001, fill_price=price_b, fee=0.01),
        ]
        _apply(session, state, order_id, fills, engine)

        position = repo.open_positions(session, "BTCUSDT")[0]
        expected_avg = (price_a * 0.001 + price_b * 0.001) / 0.002
        assert position.avg_entry_price == pytest.approx(expected_avg)
        assert position.qty == pytest.approx(0.002)

        if side == "BUY":
            assert position.stop_loss == pytest.approx(expected_avg - STOP_MULT * atr)
            assert position.take_profit == pytest.approx(expected_avg + TAKE_MULT * atr)
        else:
            assert position.stop_loss == pytest.approx(expected_avg + STOP_MULT * atr)
            assert position.take_profit == pytest.approx(expected_avg - TAKE_MULT * atr)

        risk = abs(position.avg_entry_price - position.stop_loss)
        reward = abs(position.take_profit - position.avg_entry_price)
        assert reward / risk == pytest.approx(1.5, rel=1e-9)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_two_fills_different_snapshots_recompute_after_each(session_factory, side):
    """Reprodução exata do Bloqueio 1: o segundo fill chega numa chamada
    SEPARADA de apply_order_snapshot (ex.: poller periódico re-consultando
    poll_order()) -- os níveis precisam refletir a média acumulada mesmo
    assim, não apenas dentro de uma única chamada."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 0.0, slippage_bps=0.0)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, atr = _persist_entry_order(session, side=side, qty=0.002, reference_price=100.0, atr=10.0)

        price_a, price_b = (101.0, 103.0) if side == "BUY" else (99.0, 97.0)
        _apply(session, state, order_id, [FillEvent("f1", 0.001, price_a, 0.01)], engine)

    with session_scope(session_factory) as session:
        state = session.execute(select(SystemState)).scalars().one()
        _apply(session, state, order_id, [FillEvent("f2", 0.001, price_b, 0.01)], engine)

        position = repo.open_positions(session, "BTCUSDT")[0]
        expected_avg = (price_a * 0.001 + price_b * 0.001) / 0.002
        assert position.avg_entry_price == pytest.approx(expected_avg)
        if side == "BUY":
            assert position.stop_loss == pytest.approx(expected_avg - STOP_MULT * atr)
        else:
            assert position.stop_loss == pytest.approx(expected_avg + STOP_MULT * atr)


def test_first_fill_reappearing_in_second_snapshot_is_deduplicated_and_levels_unchanged(session_factory):
    """O primeiro fill reaparecendo no segundo snapshot (poll_order() sempre
    devolve o histórico COMPLETO, nunca um delta) não pode ser reaplicado --
    nem contabilmente, nem recalculando os níveis de novo à toa."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 0.0, slippage_bps=0.0)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, atr = _persist_entry_order(session, side="BUY", qty=0.002, reference_price=100.0, atr=10.0)
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)

    with session_scope(session_factory) as session:
        state = session.execute(select(SystemState)).scalars().one()
        # snapshot completo: f1 de novo + f2 novo -- f1 deve ser ignorado
        result = _apply(
            session, state, order_id,
            [FillEvent("f1", 0.001, 101.0, 0.01), FillEvent("f2", 0.001, 103.0, 0.01)],
            engine,
        )
        assert result.new_fill_count == 1  # só f2 é novo

        position = repo.open_positions(session, "BTCUSDT")[0]
        assert position.qty == pytest.approx(0.002)  # não duplicou f1
        expected_avg = (101.0 * 0.001 + 103.0 * 0.001) / 0.002
        assert position.avg_entry_price == pytest.approx(expected_avg)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_ratio_one_point_five_after_second_and_third_fill(session_factory, side):
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 0.0, slippage_bps=0.0)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, atr = _persist_entry_order(session, side=side, qty=0.003, reference_price=100.0, atr=8.0)

        prices = [101.0, 99.0, 102.0] if side == "BUY" else [99.0, 101.0, 98.0]
        for i, p in enumerate(prices):
            _apply(session, state, order_id, [FillEvent(f"f{i}", 0.001, p, 0.01)], engine)
            position = repo.open_positions(session, "BTCUSDT")[0]
            risk = abs(position.avg_entry_price - position.stop_loss)
            reward = abs(position.take_profit - position.avg_entry_price)
            assert reward / risk == pytest.approx(1.5, rel=1e-9), f"falhou após fill {i}"


def test_late_opposite_fill_still_blocked_and_no_level_recompute(session_factory):
    """Correção v1.2 #5 preservada: um fill de entrada do lado OPOSTO ao da
    posição já aberta continua bloqueado -- e, como não é aplicado, os
    níveis da posição não são tocados."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 0.0, slippage_bps=0.0)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        buy_order_id, _ = _persist_entry_order(
            session, side="BUY", qty=0.001, reference_price=100.0, atr=10.0, idem="buy-1",
        )
        _apply(session, state, buy_order_id, [FillEvent("f1", 0.001, 100.0, 0.01)], engine)
        position_before = repo.open_positions(session, "BTCUSDT")[0]
        stop_before, take_before = position_before.stop_loss, position_before.take_profit

        sell_order_id, _ = _persist_entry_order(
            session, side="SELL", qty=0.001, reference_price=100.0, atr=10.0, idem="sell-1",
        )
        _apply(session, state, sell_order_id, [FillEvent("f2", 0.001, 100.0, 0.01)], engine)

        position_after = repo.open_positions(session, "BTCUSDT")[0]
        assert position_after.side == "BUY"  # não virou/mudou
        assert position_after.stop_loss == stop_before
        assert position_after.take_profit == take_before
        assert state.state_ambiguous is True


def test_multi_order_same_side_pyramiding_is_blocked_never_mixes_atr(session_factory):
    """Prova exigida pela correção: se uma ordem DIFERENTE contribui fill
    para o mesmo lado da mesma posição já aberta, o fluxo bloqueia em vez
    de misturar ATRs de sinais diferentes silenciosamente."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 0.0, slippage_bps=0.0)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_a_id, atr_a = _persist_entry_order(
            session, side="BUY", qty=0.001, reference_price=100.0, atr=10.0, idem="order-a",
        )
        _apply(session, state, order_a_id, [FillEvent("fa", 0.001, 100.0, 0.01)], engine)
        position_before = repo.open_positions(session, "BTCUSDT")[0]
        stop_before, take_before, qty_before = (
            position_before.stop_loss, position_before.take_profit, position_before.qty,
        )

        # Ordem DIFERENTE, mesmo símbolo/lado, ATR diferente do sinal original.
        order_b_id, atr_b = _persist_entry_order(
            session, side="BUY", qty=0.001, reference_price=105.0, atr=20.0, idem="order-b",
        )
        assert atr_a != atr_b
        result = _apply(session, state, order_b_id, [FillEvent("fb", 0.001, 105.0, 0.01)], engine)

        position_after = repo.open_positions(session, "BTCUSDT")[0]
        # Fill da ordem B NÃO foi aplicado -- posição intocada.
        assert position_after.qty == pytest.approx(qty_before)
        assert position_after.stop_loss == stop_before
        assert position_after.take_profit == take_before
        assert state.state_ambiguous is True

        events = repo.recent_security_events(session, limit=5)
        assert any(e.event_type == "MULTI_ORDER_SAME_SIDE_FILL_BLOCKED" for e in events)


def test_partial_fill_policy_untouched_by_multifill_recompute(session_factory):
    """A política de partial fill em si (quantidade preenchida, contadores)
    não muda -- só o recálculo de stop/take passa a rodar a cada fill
    aplicado."""
    engine = PaperLocalExecutionEngine(
        price_provider=lambda s: 0.0, slippage_bps=0.0, partial_fill_ratio=0.5,
    )
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, atr = _persist_entry_order(session, side="BUY", qty=0.002, reference_price=100.0, atr=10.0)
        # Simula dois fills parciais chegando em polls sucessivos.
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)

    with session_scope(session_factory) as session:
        state = session.execute(select(SystemState)).scalars().one()
        _apply(session, state, order_id, [FillEvent("f2", 0.001, 103.0, 0.01)], engine)
        position = repo.open_positions(session, "BTCUSDT")[0]
        assert position.qty == pytest.approx(0.002)  # política de quantidade intocada


# --- Bloqueio 2: sincronização de proteção remota (BYBIT_DEMO) ------------

def test_bybit_demo_creation_keeps_provisional_protection_in_order_create_payload():
    """A ordem criada continua enviando stopLoss/takeProfit do sinal como
    proteção provisória, evitando janela sem proteção -- nunca removido."""
    from tests.factories import approved_open_order

    transport = FakeBybitTransport()
    engine = _make_bybit_engine(transport)
    order = approved_open_order(symbol="BTCUSDT", side="BUY", qty=0.001, price=40000.0,
                                 stop_loss=39800.0, take_profit=40300.0)
    engine.submit(order, "idem-1", reference_price=40000.0)

    create_call = next(p for u, p in transport.post_calls if u.endswith("/v5/order/create"))
    assert create_call["stopLoss"] == "39800.00"
    assert create_call["takeProfit"] == "40300.00"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_first_fill_syncs_remote_protection_with_correct_payload_for_long_and_short(session_factory, side):
    transport = FakeBybitTransport()
    engine = _make_bybit_engine(transport)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, atr = _persist_entry_order(session, side=side, qty=0.001, reference_price=100.0, atr=10.0)
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0 if side == "BUY" else 99.0, 0.01)], engine)

        position = repo.open_positions(session, "BTCUSDT")[0]
        assert position.remote_protection_status == "SYNCED"

    sync_call = transport.trading_stop_calls[-1]
    assert sync_call["category"] == "linear"
    assert sync_call["symbol"] == "BTCUSDT"
    assert sync_call["positionIdx"] == 0
    assert sync_call["tpslMode"] == "Full"
    assert float(sync_call["stopLoss"]) == pytest.approx(position.stop_loss, abs=0.01)
    assert float(sync_call["takeProfit"]) == pytest.approx(position.take_profit, abs=0.01)


def test_second_fill_changes_average_and_syncs_protection_again(session_factory):
    transport = FakeBybitTransport()
    engine = _make_bybit_engine(transport)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, atr = _persist_entry_order(session, side="BUY", qty=0.002, reference_price=100.0, atr=10.0)
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)
        first_sync_count = len(transport.trading_stop_calls)

        _apply(session, state, order_id, [FillEvent("f2", 0.001, 103.0, 0.01)], engine)
        position = repo.open_positions(session, "BTCUSDT")[0]

    assert len(transport.trading_stop_calls) == first_sync_count + 1
    last_call = transport.trading_stop_calls[-1]
    assert float(last_call["stopLoss"]) == pytest.approx(position.stop_loss, abs=0.01)
    assert float(last_call["takeProfit"]) == pytest.approx(position.take_profit, abs=0.01)


def test_paper_local_and_paper_live_never_call_any_private_endpoint(session_factory):
    """PAPER_LOCAL/PAPER_LIVE usam o mesmo PaperLocalExecutionEngine --
    sync_position_protection() nunca chama http nenhum (não há transporte
    para chamar, é um no-op puro)."""
    engine = PaperLocalExecutionEngine(price_provider=lambda s: 0.0, slippage_bps=0.0)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, _ = _persist_entry_order(session, side="BUY", qty=0.001, reference_price=100.0, atr=10.0)
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)
        position = repo.open_positions(session, "BTCUSDT")[0]
        assert position.remote_protection_status == "SYNCED"
    # PaperLocalExecutionEngine não tem nenhum atributo de transporte HTTP --
    # a própria classe prova estruturalmente que nada remoto é alcançável.
    assert not hasattr(engine, "_http_post")
    assert not hasattr(engine, "_http_get")


def test_sync_timeout_persists_pending_blocks_new_entries_and_records_event(session_factory):
    transport = FakeBybitTransport()
    transport.fail_next_n_with_timeout = 1
    engine = _make_bybit_engine(transport)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, _ = _persist_entry_order(session, side="BUY", qty=0.001, reference_price=100.0, atr=10.0)
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)

        position = repo.open_positions(session, "BTCUSDT")[0]
        assert position.remote_protection_status == "PENDING"
        assert state.protection_sync_pending is True

        from app.risk.config import RiskLimits
        from app.risk.engine import RiskEngine
        from tests.factories import base_risk_context

        risk_engine = RiskEngine(RiskLimits(require_stop_loss=False))
        from app.strategy.schemas import Signal
        from datetime import datetime, timezone

        signal = Signal(symbol="BTCUSDT", direction="BUY", justification="t", created_at=datetime.now(timezone.utc),
                         observed_price=100.0, atr=10.0, stop_loss=90.0, take_profit=115.0)
        context = base_risk_context(protection_sync_pending=state.protection_sync_pending)
        result = risk_engine.evaluate(signal, signal_id=999, context=context)
        assert result.approved is False
        assert "proteção" in result.reason.lower()

        events = repo.recent_security_events(session, limit=5)
        assert any(e.event_type == "POSITION_PROTECTION_SYNC_FAILED" for e in events)


def test_retry_recovers_and_clears_only_the_protection_block(session_factory):
    transport = FakeBybitTransport()
    transport.fail_next_n_with_timeout = 1
    engine = _make_bybit_engine(transport)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, _ = _persist_entry_order(session, side="BUY", qty=0.001, reference_price=100.0, atr=10.0)
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)
        position = repo.open_positions(session, "BTCUSDT")[0]
        assert position.remote_protection_status == "PENDING"

        # Sem falha desta vez -- simula o retry via reconciliação/poller.
        synced = engine.sync_position_protection(
            position.symbol, position.side, position.stop_loss, position.take_profit,
        )
        assert synced is True
        position.remote_protection_status = "SYNCED"
        session.flush()
        repo.recompute_protection_sync_pending(session, state)

        assert state.protection_sync_pending is False
        # Nenhum OUTRO bloqueio foi tocado por essa recuperação.
        assert state.state_ambiguous is False
        assert state.trading_blocked is False


def test_restart_between_failure_and_retry_resumes_purely_from_the_database(session_factory):
    """Simula um reinício de processo: nenhum estado em memória sobrevive
    entre as duas `session_scope` -- a pendência só é encontrada porque foi
    persistida na Position/SystemState."""
    transport = FakeBybitTransport()
    transport.fail_next_n_with_timeout = 1
    engine = _make_bybit_engine(transport)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, _ = _persist_entry_order(session, side="BUY", qty=0.001, reference_price=100.0, atr=10.0)
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)

    # "reinício": nova sessão de banco, nenhuma referência Python reaproveitada.
    with session_scope(session_factory) as session:
        state = session.execute(select(SystemState)).scalars().one()
        assert state.protection_sync_pending is True  # sobreviveu ao "reinício"

        pending = [p for p in repo.open_positions(session) if p.remote_protection_status != "SYNCED"]
        assert len(pending) == 1
        position = pending[0]
        synced = engine.sync_position_protection(
            position.symbol, position.side, position.stop_loss, position.take_profit,
        )
        assert synced is True
        position.remote_protection_status = "SYNCED"
        session.flush()
        repo.recompute_protection_sync_pending(session, state)
        assert state.protection_sync_pending is False


def test_repeated_fill_does_not_duplicate_accounting_update(session_factory):
    transport = FakeBybitTransport()
    engine = _make_bybit_engine(transport)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, _ = _persist_entry_order(session, side="BUY", qty=0.001, reference_price=100.0, atr=10.0)
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)
        sync_calls_after_first = len(transport.trading_stop_calls)

        # mesmo fill reaparecendo (poll_order() sempre devolve o histórico completo)
        result = _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)
        assert result.new_fill_count == 0

    # Nenhuma nova tentativa de sincronização -- nada novo foi aplicado.
    assert len(transport.trading_stop_calls) == sync_calls_after_first


def test_reconciliation_detects_divergent_remote_protection():
    local = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001, "avg_entry_price": 100.0,
              "stop_loss": 80.0, "take_profit": 130.0}]
    remote = {"BTCUSDT": {"side": "BUY", "qty": 0.001, "avg_entry_price": 100.0,
                           "stop_loss": 95.0, "take_profit": 130.0}}  # stop divergente
    report = reconcile_positions(local, remote)
    assert not report.ok
    assert any("stop-loss" in m for m in report.mismatches)


def test_reconciliation_does_not_flag_missing_remote_protection_as_mismatch():
    """Ausência do campo remoto (PAPER engines, ou um retorno legado sem o
    campo) nunca é tratada como divergência -- só uma comparação onde AMBOS
    os lados reportam."""
    local = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001, "avg_entry_price": 100.0,
              "stop_loss": 80.0, "take_profit": 130.0}]
    remote = {"BTCUSDT": {"side": "BUY", "qty": 0.001, "avg_entry_price": 100.0}}  # sem stop/take
    report = reconcile_positions(local, remote)
    assert report.ok


# --- Histórico: nenhuma posição antiga é reescrita -------------------------

def test_no_historical_position_rewritten_by_multifill_or_protection_sync(session_factory):
    with session_scope(session_factory) as session:
        old_position = repo.open_position(
            session, "ETHUSDT", "BUY", 0.01, 2000.0, stop_loss=1900.0, take_profit=2200.0,
        )
        repo.close_position(session, old_position, realized_pnl_delta=3.0, closing_fee=0.02)
        old_id = old_position.id
        snapshot_before = (
            old_position.stop_loss, old_position.take_profit, old_position.avg_entry_price,
            old_position.remote_protection_status, old_position.status,
        )

    transport = FakeBybitTransport()
    engine = _make_bybit_engine(transport)
    with session_scope(session_factory) as session:
        state = _fresh_state(session)
        order_id, _ = _persist_entry_order(
            session, side="BUY", qty=0.002, reference_price=100.0, atr=10.0, symbol="BTCUSDT",
        )
        _apply(session, state, order_id, [FillEvent("f1", 0.001, 101.0, 0.01)], engine)
        _apply(session, state, order_id, [FillEvent("f2", 0.001, 103.0, 0.01)], engine)

    with session_scope(session_factory) as session:
        reloaded = session.get(Position, old_id)
        snapshot_after = (
            reloaded.stop_loss, reloaded.take_profit, reloaded.avg_entry_price,
            reloaded.remote_protection_status, reloaded.status,
        )
        assert snapshot_after == snapshot_before
