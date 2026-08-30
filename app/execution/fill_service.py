"""Correção v1.1 #1/#2/#4: the SINGLE place any fill is ever applied to an
order and a position. Used by every caller that can learn about a fill --
the immediate post-submit poll, the periodic open-order poller, a
kill-switch cancellation race, and reconciliation -- so there is exactly
one code path, never a second one that applies things differently (the
audited gap in v1.0: the kill switch called `repo.record_fill` directly and
never touched `Execution`/`Position`/session counters at all).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.execution import fill_ledger
from app.execution.base import OrderStatusSnapshot
from app.execution.order_state import IllegalOrderTransitionError, OrderStatus, is_terminal
from app.persistence import repo
from app.persistence.models import Order, OperationalSession, SystemState
from app.sessions import increment as increment_session_counter


@dataclass(frozen=True)
class FillApplicationResult:
    status: OrderStatus
    new_fill_count: int
    realized_pnl_delta_total: float = 0.0
    closed_fully: bool | None = None  # None when is_close=False or no fill applied yet


def apply_order_snapshot(
    session, state: SystemState, op_session: OperationalSession | None, order: Order,
    snapshot: OrderStatusSnapshot, is_close: bool, max_api_failures: int,
) -> FillApplicationResult:
    """Reconciles `order`'s persisted status/fills with `snapshot` (from
    `ExecutionEngine.poll_order()`):
    1. Transitions `order.status` to `snapshot.status` if it actually
       changed (never a no-op transition into the same terminal state --
       `transition_order_status` already rejects that, so this only
       attempts it when needed).
    2. Records any NEW fills via the idempotent ledger
       (`fill_ledger.record_new_fills` -- already-seen `exchange_fill_id`s
       are silently skipped), then applies each new fill's DELTA to the
       position: opens/adds for an entry order, reduces/closes (with
       realized PnL) for a close order.
    3. Refreshes `SystemState.order_state_unknown` and recomputes
       `trading_blocked` accordingly.
    """
    current = OrderStatus(order.status)
    if snapshot.status != current:
        if snapshot.exchange_order_id and not order.exchange_order_id:
            order.exchange_order_id = snapshot.exchange_order_id
        try:
            repo.transition_order_status(
                session, order, snapshot.status,
                detail=f"poll_order() reportou {snapshot.status.value}.",
            )
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
        increment_session_counter(op_session, "fills_count")
        existing = repo.open_positions(session, order.symbol)
        position = existing[0] if existing else None

        for row in new_rows:
            if not is_close:
                if position is None:
                    position = repo.open_position(
                        session, order.symbol, order.side, row.fill_qty, row.fill_price,
                        order.stop_loss, order.take_profit, opening_fee=row.fee,
                    )
                else:
                    repo.add_to_position(session, position, row.fill_qty, row.fill_price, row.fee)
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
