"""Correção v1.1 #1/#2/#4: the SINGLE place any fill is ever applied to an
order and a position. Used by every caller that can learn about a fill --
the immediate post-submit poll, the periodic open-order poller, a
kill-switch cancellation race, and reconciliation -- so there is exactly
one code path, never a second one that applies things differently (the
audited gap in v1.0: the kill switch called `repo.record_fill` directly and
never touched `Execution`/`Position`/session counters at all).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from app.execution import fill_ledger
from app.execution.base import OrderStatusSnapshot
from app.execution.order_state import IllegalOrderTransitionError, OrderStatus, is_terminal
from app.persistence import repo
from app.persistence.models import Execution, Order, OperationalSession, StrategySignal, SystemState
from app.sessions import increment as increment_session_counter

# Correção Cirúrgica do Stop/Take Pós-Preenchimento: devem bater com
# StrategyConfig.stop_loss_atr_multiple / take_profit_atr_multiple
# (app/strategy/engine.py) -- a mesma razão 1,5:1 usada no sinal, aplicada
# de novo aqui, agora sobre o preço real de preenchimento em vez do preço
# do sinal. Duplicados aqui (em vez de importar StrategyConfig) para manter
# a camada de execução/preenchimento sem depender do módulo de estratégia.
_STOP_LOSS_ATR_MULTIPLE = 2.0
_TAKE_PROFIT_ATR_MULTIPLE = 3.0


def _stop_target_from_fill(session, order: Order, fill_price: float) -> tuple[float | None, float | None]:
    """Correção Cirúrgica do Stop/Take Pós-Preenchimento (achado da auditoria
    de 31/08-01/09/2026): antes desta correção, o stop-loss e o take-profit
    persistidos na posição vinham direto de `order.stop_loss`/
    `order.take_profit` -- calculados pelo motor de estratégia sobre o preço
    do SINAL, antes de qualquer slippage. Como o slippage simulado
    (`app/execution/paper_local.py::submit`) é sempre adverso, isso deixava
    os níveis persistidos sistematicamente deslocados em relação ao preço
    real de entrada, degradando a razão risco:retorno nominal de 1,5:1 para
    algo bem pior em todo trade.

    Esta função recalcula os níveis DEFINITIVOS a partir de `fill_price` (o
    preço real -- para o primeiro fill que abre uma posição, exatamente o
    preço médio de preenchimento dessa posição), preservando o ATR
    exatamente como calculado no sinal que originou a ordem (nunca
    recalculado a partir de candles posteriores) -- lido diretamente de
    `StrategySignal.atr` via `Order.risk_evaluation.signal_id`, nunca
    re-derivado por aritmética reversa.

    Nunca produz NaN/infinito/negativo: qualquer ATR ausente, não-finito ou
    <= 0 (nunca deveria acontecer -- uma ordem só existe se o sinal já
    passou pelo filtro de volatilidade ATR, que exige ATR > 0 -- mas
    verificado aqui defensivamente) faz esta função devolver os níveis
    originais da ordem sem alteração, em vez de arriscar um valor inválido.
    """
    if order.stop_loss is None or order.take_profit is None:
        return order.stop_loss, order.take_profit

    risk_evaluation = order.risk_evaluation
    atr = None
    if risk_evaluation is not None:
        atr = session.execute(
            select(StrategySignal.atr).where(StrategySignal.id == risk_evaluation.signal_id)
        ).scalar_one_or_none()

    if atr is None or not math.isfinite(atr) or atr <= 0:
        return order.stop_loss, order.take_profit

    if order.side == "BUY":
        stop_loss = fill_price - _STOP_LOSS_ATR_MULTIPLE * atr
        take_profit = fill_price + _TAKE_PROFIT_ATR_MULTIPLE * atr
    else:
        stop_loss = fill_price + _STOP_LOSS_ATR_MULTIPLE * atr
        take_profit = fill_price - _TAKE_PROFIT_ATR_MULTIPLE * atr

    if not (math.isfinite(stop_loss) and math.isfinite(take_profit)):
        return order.stop_loss, order.take_profit

    return stop_loss, take_profit


_POSITION_OPENED_AT_RACE_TOLERANCE = timedelta(seconds=5)


def _position_has_fills_from_a_different_order(session, position, order: Order) -> bool:
    """Correção Stop/Take Pós-Preenchimento v1.1, Bloqueio 1: garante que a
    posição aberta não está sendo alimentada por ATRs de sinais diferentes
    silenciosamente. Um segundo fill da MESMA ordem (múltiplos fills/
    snapshots) sempre passa por aqui livre -- só uma ordem DIFERENTE
    contribuindo para o mesmo lado da mesma posição já aberta é bloqueada:
    nunca escolhe um ATR arbitrário entre os dois sinais, nunca pirâmida
    silenciosamente."""
    other_order_id = session.execute(
        select(Execution.order_id)
        .join(Order, Execution.order_id == Order.id)
        .where(
            Order.symbol == position.symbol, Order.side == position.side,
            Order.is_close.is_(False), Execution.order_id != order.id,
            # A tolerância cobre a ordem de gravação DENTRO desta função: o
            # fill da ordem que abriu a posição é registrado (fill_ledger)
            # ANTES de `position.opened_at` ser gravado (repo.open_position
            # roda depois, na mesma chamada) -- sem a folga, o próprio fill
            # de abertura ficaria fora da janela e nunca seria encontrado
            # como "já pertence a esta posição".
            Execution.executed_at >= position.opened_at - _POSITION_OPENED_AT_RACE_TOLERANCE,
        )
        .limit(1)
    ).scalar_one_or_none()
    return other_order_id is not None


def _sync_remote_protection(session, state: SystemState, execution_engine, order: Order, position) -> None:
    """Correção Stop/Take Pós-Preenchimento v1.1, Bloqueio 2: ponto único de
    sincronização da proteção remota (stop/alvo) -- chamado depois de
    QUALQUER recálculo de níveis (abertura ou fill adicional aplicado),
    pelos mesmos 4 caminhos que descobrem fills e chamam
    `apply_order_snapshot` (submit+poll imediato, poller periódico, corrida
    do kill switch, reconciliação), sempre pela mesma função -- nunca
    espalhada. PAPER_LOCAL/PAPER_LIVE nunca chamam nada remoto
    (`ExecutionEngine.sync_position_protection` é um no-op que sempre
    retorna True nesses modos -- ver app/execution/paper_local.py).
    BYBIT_DEMO é o único modo que realmente chama a corretora (`POST
    /v5/position/trading-stop`).

    Uma falha nunca é silenciosa: `position.remote_protection_status` fica
    'PENDING', `SystemState.protection_sync_pending` (persistido, nunca só
    em memória) passa a bloquear novas entradas (RiskContext.
    protection_sync_pending, entry-only -- nunca bloqueia fechamento), e um
    evento de segurança é registrado. O retry é feito pela reconciliação
    periódica (Orchestrator.reconcile()), que já roda no boot e
    periodicamente -- nunca depende de estado em memória para retomar após
    um reinício."""
    synced = execution_engine.sync_position_protection(
        order.symbol, position.side, position.stop_loss, position.take_profit,
    )
    position.remote_protection_status = "SYNCED" if synced else "PENDING"
    session.flush()  # a sessão é autoflush=False -- sem isto, o recompute abaixo lê o valor antigo
    if not synced:
        detail = (
            f"Sincronização da proteção remota (stop/alvo) falhou para a posição {position.id} "
            f"({order.symbol} {position.side}); novas aberturas de posição bloqueadas até confirmação."
        )
        repo.record_security_event(session, "POSITION_PROTECTION_SYNC_FAILED", detail)
    repo.recompute_protection_sync_pending(session, state)


@dataclass(frozen=True)
class FillApplicationResult:
    status: OrderStatus
    new_fill_count: int
    realized_pnl_delta_total: float = 0.0
    closed_fully: bool | None = None  # None when is_close=False or no fill applied yet


def apply_order_snapshot(
    session, state: SystemState, op_session: OperationalSession | None, order: Order,
    snapshot: OrderStatusSnapshot, is_close: bool, max_api_failures: int, execution_engine=None,
) -> FillApplicationResult:
    """Reconciles `order`'s persisted status/fills with `snapshot` (from
    `ExecutionEngine.poll_order()`):
    1. Correção v1.2 #1: a TERMINAL `snapshot.status` (FILLED/CANCELLED/
       REJECTED) is only ever persisted once `snapshot.fills_complete` is
       True -- the audited defect was exactly this: `poll_order()` could
       report `status=FILLED` with `fills=[]` after an execution-list
       timeout, `apply_order_snapshot` would still persist FILLED, and
       `repo.non_terminal_orders()` would then drop the order from the
       recoverable set forever, permanently losing its fills/position/fees.
       When `fills_complete=False`, `order.status` is deliberately left
       UNCHANGED (so it stays non-terminal and therefore stays selected by
       `repo.non_terminal_orders()` for the next poll) -- only
       `order.pending_exchange_status`/`order.fills_sync_status` record
       what the exchange reported, as an audit trail, never as the
       authoritative status. The eventual real transition (once a later
       poll proves `fills_complete=True`) applies the terminal status AND
       every gathered fill together, in this same function call/transaction
       -- there is no window where one is persisted without the other.
    2. Records any NEW fills via the idempotent ledger
       (`fill_ledger.record_new_fills` -- already-seen `exchange_fill_id`s
       are silently skipped) REGARDLESS of `fills_complete` -- fills
       already validated before an interruption are never held back or
       discarded, only the terminal status transition is deferred. Applies
       each new fill's DELTA to the position: opens/adds for an entry
       order, reduces/closes (with realized PnL) for a close order.
       Correção v1.2 #5: an entry fill (`is_close=False`) is NEVER summed
       onto a position on the OPPOSITE side -- a late/opposite fill is
       blocked (never fabricated into the wrong position) and flagged as a
       security event + ambiguous state, requiring reconciliation.
    3. Refreshes `SystemState.order_state_unknown` and recomputes
       `trading_blocked` accordingly.
    """
    current = OrderStatus(order.status)
    if is_terminal(snapshot.status) and not snapshot.fills_complete:
        # Correção v1.2 #1: never terminalize before the fill history is
        # proven complete -- record the exchange's claim for audit/
        # observability, but leave `order.status` exactly where it is.
        if snapshot.exchange_order_id and not order.exchange_order_id:
            order.exchange_order_id = snapshot.exchange_order_id
        order.pending_exchange_status = snapshot.status.value
        order.fills_sync_status = "PENDING"
    elif snapshot.status != current:
        if snapshot.exchange_order_id and not order.exchange_order_id:
            order.exchange_order_id = snapshot.exchange_order_id
        try:
            repo.transition_order_status(
                session, order, snapshot.status,
                detail=f"poll_order() reportou {snapshot.status.value}.",
            )
            if is_terminal(snapshot.status):
                order.fills_sync_status = "COMPLETE"
                order.pending_exchange_status = None
        except IllegalOrderTransitionError:
            # A snapshot arriving out of order (e.g. a stale poll racing a
            # newer one) is reported as UNKNOWN rather than silently
            # ignored or crashing the tick/poller loop.
            if not is_terminal(OrderStatus(order.status)):
                repo.transition_order_status(
                    session, order, OrderStatus.UNKNOWN,
                    detail="Transição de status inesperada durante poll_order().",
                )

    new_rows = fill_ledger.record_new_fills(session, order, snapshot.fills)
    realized_pnl_delta_total = 0.0
    closed_fully: bool | None = None

    if new_rows:
        increment_session_counter(op_session, "fills_count", by=len(new_rows))
        existing = repo.open_positions(session, order.symbol)
        position = existing[0] if existing else None

        for row in new_rows:
            if not is_close:
                if position is None:
                    # Correção Cirúrgica do Stop/Take Pós-Preenchimento:
                    # níveis derivados de `row.fill_price` (preço real desta
                    # abertura), não de `order.stop_loss`/`order.take_profit`
                    # (preço do sinal, pré-slippage).
                    stop_loss, take_profit = _stop_target_from_fill(session, order, row.fill_price)
                    position = repo.open_position(
                        session, order.symbol, order.side, row.fill_qty, row.fill_price,
                        stop_loss, take_profit, opening_fee=row.fee,
                    )
                    if execution_engine is not None:
                        _sync_remote_protection(session, state, execution_engine, order, position)
                elif position.side != order.side:
                    # Correção v1.2 #5: a late/opposite fill -- e.g. the
                    # position already flipped/closed by the time this fill
                    # arrived. Never fabricate a state by summing onto the
                    # wrong side; block and require reconciliation instead.
                    state.state_ambiguous = True
                    detail = (
                        f"Fill de entrada ({order.side}) da ordem {order.id} recebido, mas a posição "
                        f"aberta em {order.symbol} está do lado {position.side} -- fill NÃO aplicado "
                        "à posição (bloqueio de segurança); requer reconciliação manual."
                    )
                    repo.record_security_event(session, "LATE_OPPOSITE_FILL_BLOCKED", detail)
                elif _position_has_fills_from_a_different_order(session, position, order):
                    # Correção Stop/Take Pós-Preenchimento v1.1, Bloqueio 1:
                    # esta posição já foi alimentada por uma ordem DIFERENTE
                    # -- nunca mistura ATRs de sinais distintos silenciosamente
                    # nem pirâmida; bloqueia e exige reconciliação manual.
                    state.state_ambiguous = True
                    detail = (
                        f"Fill de entrada ({order.side}) da ordem {order.id} recebido para a posição "
                        f"{position.id} ({order.symbol}), que já tem fills de uma ordem diferente -- fill "
                        "NÃO aplicado (bloqueio de segurança contra piramidagem/ATR misto); requer "
                        "reconciliação manual."
                    )
                    repo.record_security_event(session, "MULTI_ORDER_SAME_SIDE_FILL_BLOCKED", detail)
                else:
                    repo.add_to_position(session, position, row.fill_qty, row.fill_price, row.fee)
                    # Bloqueio 1: cada novo fill de entrada aplicado
                    # recalcula os níveis sobre o avg_entry_price já
                    # ponderado por TODOS os fills, dentro da mesma
                    # transação -- nunca fica ancorado no fill isolado
                    # anterior.
                    stop_loss, take_profit = _stop_target_from_fill(session, order, position.avg_entry_price)
                    position.stop_loss = stop_loss
                    position.take_profit = take_profit
                    session.flush()
                    if execution_engine is not None:
                        _sync_remote_protection(session, state, execution_engine, order, position)
            else:
                if position is None:
                    # Nothing local to reduce/close -- reconciliation is what
                    # will flag this divergence; fill_service never fabricates
                    # a position to apply a close fill against.
                    continue
                direction = 1 if position.side == "BUY" else -1
                delta = direction * (row.fill_price - position.avg_entry_price) * row.fill_qty
                realized_pnl_delta_total += delta
                fully = row.fill_qty >= position.qty - 1e-9
                if fully:
                    repo.close_position(session, position, delta, row.fee)
                    closed_fully = True
                    position = None
                else:
                    repo.reduce_position(session, position, row.fill_qty, delta, row.fee)
                    closed_fully = False

    state.order_state_unknown = repo.has_unknown_orders(session)
    repo.recompute_trading_blocked(state, max_api_failures)

    return FillApplicationResult(
        status=OrderStatus(order.status), new_fill_count=len(new_rows),
        realized_pnl_delta_total=realized_pnl_delta_total, closed_fully=closed_fully,
    )
