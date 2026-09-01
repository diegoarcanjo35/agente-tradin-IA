"""Correção Cirúrgica do Stop/Take Pós-Preenchimento -- reprodução auditada:
stop-loss e take-profit eram calculados sobre o preço do SINAL, antes do
slippage. Como o slippage simulado é sempre adverso
(`app/execution/paper_local.py::submit`), os níveis persistidos ficavam
sistematicamente deslocados em relação ao preço real de entrada, degradando
a razão risco:retorno nominal de 1,5:1 (stop=2xATR, alvo=3xATR) para algo
bem pior em todo trade real observado em PAPER_LIVE.

Correção: `app/execution/fill_service.py::_stop_target_from_fill` recalcula
os níveis definitivos a partir do preço médio REAL de preenchimento,
preservando o ATR do sinal (lido de `StrategySignal.atr`, nunca
recalculado).
"""
from __future__ import annotations

import math

import pytest
from sqlalchemy import select

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.core.clock import ReplayClockProvider
from app.core.config import RunMode, Settings
from app.execution.base import FillEvent, OrderStatusSnapshot, SubmitAck
from app.execution.fill_service import _stop_target_from_fill
from app.execution.order_state import OrderStatus
from app.execution.paper_local import PaperLocalExecutionEngine
from app.orchestrator import Orchestrator
from app.persistence import repo
from app.persistence.db import session_scope
from app.persistence.models import Order, StrategySignal
from app.risk.engine import RiskEngine
from app.risk.config import RiskLimits
from app.strategy.engine import StrategyConfig, StrategyEngine
from tests.factories import activate_operational_state
from tests.test_price_correctness import ListMarketDataProvider, make_candle

STOP_MULT = 2.0
TAKE_MULT = 3.0

# Mesmo padrão de tests/test_price_correctness.py: períodos curtos para
# disparar o cruzamento rapidamente e de forma determinística.
LONG_PRICES = [100, 99, 98, 97, 96, 500]   # tendência de baixa -> salto -> cruzamento de alta (BUY)
SHORT_PRICES = [100, 101, 102, 103, 104, 90]  # tendência de alta -> queda -> cruzamento de baixa (SELL)


def _build_orchestrator(session_factory, candles, slippage_bps, partial_fill_ratio=None):
    settings = Settings(mode=RunMode.REPLAY)
    market_data_provider = ListMarketDataProvider(candles)
    cfg = StrategyConfig(
        fast_period=2, slow_period=4, atr_period=2,
        min_atr_pct_of_price=0.0, max_atr_pct_of_price=1.0,
        stop_loss_atr_multiple=STOP_MULT, take_profit_atr_multiple=TAKE_MULT,
    )
    strategy_engine = StrategyEngine(symbol=settings.symbol, config=cfg)
    risk_engine = RiskEngine(RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0,
                                         require_stop_loss=False))
    price_state: dict[str, float] = {}
    execution_engine = PaperLocalExecutionEngine(
        price_provider=lambda s: price_state.get(s, 0.0), slippage_bps=slippage_bps,
        partial_fill_ratio=partial_fill_ratio,
    )
    ai_agent = AIShadowAgent(provider=SimulatedProvider(), enabled=False)
    orch = Orchestrator(
        settings=settings, session_factory=session_factory,
        market_data_provider=market_data_provider, strategy_engine=strategy_engine,
        risk_engine=risk_engine, execution_engine=execution_engine, ai_agent=ai_agent,
        clock_provider=ReplayClockProvider(drift_seconds=0.0), price_state=price_state,
    )
    activate_operational_state(orch)
    return orch


def _run_until_first_open(orch, candles):
    for _ in range(len(candles) + 1):
        result = orch.tick()
        if result["status"] == "order_filled":
            return result
        if result["status"] == "no_data":
            break
    raise AssertionError("nenhuma ordem de abertura foi preenchida durante o teste")


def _open_order_position_and_signal(session_factory):
    with session_scope(session_factory) as session:
        order = session.execute(select(Order).where(Order.is_close.is_(False))).scalars().one()
        position = repo.open_positions(session, order.symbol)[0]
        signal = session.execute(
            select(StrategySignal).where(StrategySignal.id == order.risk_evaluation.signal_id)
        ).scalar_one()
        return (
            order.side, order.reference_price, order.avg_fill_price, order.qty,
            position.avg_entry_price, position.stop_loss, position.take_profit,
            signal.atr,
        )


# --- Reprodução: prova de que o preço do fill diverge do preço do sinal --

@pytest.mark.parametrize("prices,side", [(LONG_PRICES, "BUY"), (SHORT_PRICES, "SELL")])
def test_slippage_moves_the_fill_away_from_the_signal_price(session_factory, prices, side):
    candles = [make_candle(i, p) for i, p in enumerate(prices)]
    orch = _build_orchestrator(session_factory, candles, slippage_bps=50.0)
    _run_until_first_open(orch, candles)

    order_side, reference_price, avg_fill_price, *_ = _open_order_position_and_signal(session_factory)
    assert order_side == side
    assert avg_fill_price != pytest.approx(reference_price)
    if side == "BUY":
        assert avg_fill_price > reference_price  # slippage sempre adverso na compra
    else:
        assert avg_fill_price < reference_price  # slippage sempre adverso na venda


# --- Pós-correção: níveis definitivos derivados do preço de preenchimento -

@pytest.mark.parametrize("prices,side", [(LONG_PRICES, "BUY"), (SHORT_PRICES, "SELL")])
def test_stop_and_target_are_derived_from_the_fill_price_not_the_signal_price(session_factory, prices, side):
    candles = [make_candle(i, p) for i, p in enumerate(prices)]
    orch = _build_orchestrator(session_factory, candles, slippage_bps=50.0)
    _run_until_first_open(orch, candles)

    (order_side, reference_price, avg_fill_price, qty, entry_price,
     stop_loss, take_profit, atr) = _open_order_position_and_signal(session_factory)

    assert order_side == side
    assert entry_price == pytest.approx(avg_fill_price)  # preço-base definitivo = fill real

    if side == "BUY":
        expected_stop = entry_price - STOP_MULT * atr
        expected_take = entry_price + TAKE_MULT * atr
    else:
        expected_stop = entry_price + STOP_MULT * atr
        expected_take = entry_price - TAKE_MULT * atr

    assert stop_loss == pytest.approx(expected_stop)
    assert take_profit == pytest.approx(expected_take)

    # Nunca mais ancorado no preço do sinal (a menos que, por coincidência,
    # não tenha havido slippage nenhum -- não é o caso aqui).
    wrong_stop_from_signal = (
        reference_price - STOP_MULT * atr if side == "BUY" else reference_price + STOP_MULT * atr
    )
    assert stop_loss != pytest.approx(wrong_stop_from_signal)


@pytest.mark.parametrize("prices,side", [(LONG_PRICES, "BUY"), (SHORT_PRICES, "SELL")])
def test_risk_reward_ratio_is_restored_to_one_point_five(session_factory, prices, side):
    candles = [make_candle(i, p) for i, p in enumerate(prices)]
    orch = _build_orchestrator(session_factory, candles, slippage_bps=50.0)
    _run_until_first_open(orch, candles)

    _, _, _, _, entry_price, stop_loss, take_profit, _ = _open_order_position_and_signal(session_factory)

    risk = abs(entry_price - stop_loss)
    reward = abs(take_profit - entry_price)
    assert reward / risk == pytest.approx(1.5, rel=1e-9)


def test_signal_price_is_preserved_and_never_overwritten_by_the_fill(session_factory):
    candles = [make_candle(i, p) for i, p in enumerate(LONG_PRICES)]
    orch = _build_orchestrator(session_factory, candles, slippage_bps=50.0)
    _run_until_first_open(orch, candles)

    with session_scope(session_factory) as session:
        order = session.execute(select(Order).where(Order.is_close.is_(False))).scalars().one()
        # reference_price (preço do sinal) e avg_fill_price (preço real)
        # continuam sendo dois campos distintos e ambos preservados -- a
        # correção nunca sobrescreve um com o outro.
        assert order.reference_price is not None
        assert order.avg_fill_price is not None
        assert order.reference_price != pytest.approx(order.avg_fill_price)
        # E o stop/take originais do SINAL (pré-correção) continuam
        # persistidos no próprio Order, intocados -- só a Position usa os
        # níveis recalculados.
        assert order.stop_loss is not None and order.take_profit is not None


def test_order_fill_and_position_remain_correctly_linked(session_factory):
    candles = [make_candle(i, p) for i, p in enumerate(LONG_PRICES)]
    orch = _build_orchestrator(session_factory, candles, slippage_bps=50.0)
    _run_until_first_open(orch, candles)

    with session_scope(session_factory) as session:
        order = session.execute(select(Order).where(Order.is_close.is_(False))).scalars().one()
        position = repo.open_positions(session, order.symbol)[0]
        executions = list(order.executions)
        assert len(executions) == 1
        assert executions[0].fill_price == pytest.approx(order.avg_fill_price)
        assert position.qty == pytest.approx(order.filled_qty)
        assert position.avg_entry_price == pytest.approx(order.avg_fill_price)


# --- Sem fill: nenhuma posição, nenhum nível fictício ---------------------

def test_no_fill_creates_no_position_and_no_levels(session_factory):
    """poll_order() sem fills (ordem ainda não confirmada): fill_service não
    deve criar posição nem inventar stop/take -- comportamento preexistente,
    apenas confirmado como não regredido por esta correção."""
    from app.persistence.models import OperationalSession, SystemState
    from app.execution import fill_service

    with session_scope(session_factory) as session:
        state = SystemState()
        session.add(state)
        session.flush()

        from tests.factories import approved_open_order
        from app.execution.idempotency import make_idempotency_key

        approved = approved_open_order(symbol="BTCUSDT", side="BUY", qty=0.01, price=100.0,
                                        stop_loss=90.0, take_profit=115.0, signal_id=1)
        order = Order(
            idempotency_key=make_idempotency_key(approved, "no-fill-test"),
            risk_evaluation_id=1, symbol="BTCUSDT", side="BUY", qty=0.01,
            stop_loss=90.0, take_profit=115.0, is_close=False, status=OrderStatus.SUBMITTED.value,
            mode="REPLAY", reference_price=100.0,
        )
        session.add(order)
        session.flush()

        snapshot = OrderStatusSnapshot(exchange_order_id="X", status=OrderStatus.SUBMITTED, fills=[])
        fill_service.apply_order_snapshot(session, state, None, order, snapshot, is_close=False, max_api_failures=5)

        assert repo.open_positions(session, "BTCUSDT") == []


# --- Partial fill: usa o preço médio efetivamente aceito, política intocada

def test_partial_fill_uses_the_accepted_average_fill_price(session_factory):
    candles = [make_candle(i, p) for i, p in enumerate(LONG_PRICES)]
    orch = _build_orchestrator(session_factory, candles, slippage_bps=50.0, partial_fill_ratio=0.4)
    _run_until_first_open(orch, candles)

    (order_side, reference_price, avg_fill_price, qty, entry_price,
     stop_loss, take_profit, atr) = _open_order_position_and_signal(session_factory)

    assert entry_price == pytest.approx(avg_fill_price)
    expected_stop = entry_price - STOP_MULT * atr
    expected_take = entry_price + TAKE_MULT * atr
    assert stop_loss == pytest.approx(expected_stop)
    assert take_profit == pytest.approx(expected_take)


# --- Guarda contra ATR/valores inválidos -----------------------------------

class _FakeRiskEval:
    def __init__(self, signal_id):
        self.signal_id = signal_id


class _FakeOrder:
    def __init__(self, side, stop_loss, take_profit, risk_evaluation):
        self.side = side
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.risk_evaluation = risk_evaluation


def test_missing_signal_falls_back_to_original_levels_never_raises(session_factory):
    with session_scope(session_factory) as session:
        order = _FakeOrder("BUY", 90.0, 115.0, _FakeRiskEval(signal_id=999999))  # sinal inexistente
        stop_loss, take_profit = _stop_target_from_fill(session, order, fill_price=105.0)
        assert stop_loss == 90.0
        assert take_profit == 115.0


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSessionReturning:
    """Evita gravar NaN/inf de verdade no SQLite (o driver não os
    round-tripa de forma confiável) -- testa a guarda diretamente contra o
    valor que `session.execute(...).scalar_one_or_none()` devolveria."""

    def __init__(self, value):
        self._value = value

    def execute(self, _stmt):
        return _FakeScalarResult(self._value)


@pytest.mark.parametrize("bad_atr", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_atr_falls_back_to_original_levels_never_produces_nan_or_inf(bad_atr):
    session = _FakeSessionReturning(bad_atr)
    order = _FakeOrder("BUY", 90.0, 115.0, _FakeRiskEval(signal_id=1))
    stop_loss, take_profit = _stop_target_from_fill(session, order, fill_price=105.0)

    assert stop_loss == 90.0
    assert take_profit == 115.0
    assert math.isfinite(stop_loss) and math.isfinite(take_profit)


def test_no_stop_loss_on_order_is_passed_through_unchanged(session_factory):
    """Ordem sem stop/take (ex.: require_stop_loss=False e sinal sem
    níveis) -- nada a recalcular, comportamento antigo preservado."""
    with session_scope(session_factory) as session:
        order = _FakeOrder("BUY", None, None, None)
        stop_loss, take_profit = _stop_target_from_fill(session, order, fill_price=105.0)
        assert stop_loss is None
        assert take_profit is None


# --- Limites de risco continuam respeitados após o recálculo --------------

@pytest.mark.parametrize("prices,side", [(LONG_PRICES, "BUY"), (SHORT_PRICES, "SELL")])
def test_position_sizing_is_untouched_by_this_correction(session_factory, prices, side):
    """`qty` é decidido pelo Risk Engine ANTES do fill, a partir do preço do
    sinal (`app/risk/engine.py:237: qty = position_usd / signal.observed_price`)
    -- esta correção nunca toca `qty`/dimensionamento, só `stop_loss`/
    `take_profit`. A exposição real (qty * preço de preenchimento) pode
    divergir ligeiramente do limite configurado por causa do slippage --
    isso já acontecia antes desta correção e é orthogonal a ela (não foi
    introduzido nem agravado por recalcular stop/take)."""
    candles = [make_candle(i, p) for i, p in enumerate(prices)]
    orch = _build_orchestrator(session_factory, candles, slippage_bps=50.0)
    _run_until_first_open(orch, candles)

    with session_scope(session_factory) as session:
        order = session.execute(select(Order).where(Order.is_close.is_(False))).scalars().one()
        position = repo.open_positions(session, "BTCUSDT")[0]

        exposure_at_reference = order.qty * order.reference_price
        exposure_at_fill = position.qty * position.avg_entry_price

        assert order.qty == pytest.approx(position.qty)  # qty nunca alterado por esta correção
        assert exposure_at_reference <= 50.0 + 1e-6  # limite respeitado no preço do sinal (Risk Engine)
        # o desvio pelo preço real de preenchimento é limitado ao slippage configurado (50 bps aqui)
        assert exposure_at_fill <= 50.0 * 1.01


# --- Histórico: trades antigos nunca são reescritos ------------------------

def test_pre_existing_closed_position_is_never_rewritten(session_factory):
    """Uma posição já fechada, com stop/take calculados pela regra antiga
    (preço do sinal), deve permanecer byte-a-byte igual depois de um novo
    trade ser aberto e fechado sob a regra nova."""
    with session_scope(session_factory) as session:
        old_position = repo.open_position(
            session, "BTCUSDT", "BUY", 0.001, 100.0, stop_loss=80.0, take_profit=130.0,
        )
        repo.close_position(session, old_position, realized_pnl_delta=5.0, closing_fee=0.01)
        old_id = old_position.id
        old_snapshot = (
            old_position.stop_loss, old_position.take_profit, old_position.avg_entry_price,
            old_position.realized_pnl, old_position.status, old_position.closed_at,
        )

    candles = [make_candle(i, p) for i, p in enumerate(LONG_PRICES)]
    orch = _build_orchestrator(session_factory, candles, slippage_bps=50.0)
    _run_until_first_open(orch, candles)

    with session_scope(session_factory) as session:
        from app.persistence.models import Position

        reloaded = session.get(Position, old_id)
        new_snapshot = (
            reloaded.stop_loss, reloaded.take_profit, reloaded.avg_entry_price,
            reloaded.realized_pnl, reloaded.status, reloaded.closed_at,
        )
        assert new_snapshot == old_snapshot


# --- Versionamento / fingerprint da sessão ---------------------------------

def test_strategy_version_constant_was_bumped_for_this_correction():
    from app.api.main import STRATEGY_VERSION

    assert STRATEGY_VERSION == "v1.1"


def test_bumped_strategy_version_ends_old_session_and_starts_a_new_one(tmp_path):
    from app.persistence.db import init_db, make_engine, make_session_factory
    from app.sessions import start_or_resume_session

    engine = make_engine(f"sqlite:///{tmp_path / 'fp.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    settings = Settings(mode=RunMode.REPLAY, symbol="BTCUSDT", database_url="sqlite:///:memory:")
    limits = RiskLimits(max_position_usd=50.0, max_concurrent_positions=1, max_daily_loss_usd=25.0,
                         max_total_exposure_usd=50.0, cooldown_after_losses=3, cooldown_minutes=30,
                         max_data_staleness_seconds=30, max_api_failures=5, max_clock_drift_seconds=5.0)

    with session_scope(session_factory) as session:
        old = start_or_resume_session(session, settings, "v1", limits)
        old_id, old_uid = old.id, old.session_uid

    with session_scope(session_factory) as session:
        new = start_or_resume_session(session, settings, "v1.1", limits)
        assert new.id != old_id
        assert new.session_uid != old_uid
        assert new.ended_at is None

    with session_scope(session_factory) as session:
        from app.persistence.models import OperationalSession

        old_reloaded = session.get(OperationalSession, old_id)
        assert old_reloaded.ended_at is not None
        assert "configura" in old_reloaded.end_reason.lower()
