"""Correção v1.1 #2: persistent, idempotent fill ledger. Fills are recorded
individually, deduplicated by the exchange's own `exchange_fill_id`
(enforced structurally by a UNIQUE(order_id, exchange_fill_id) index --
migration v4), and an order's cumulative totals are always RE-DERIVED from
the full set of recorded fills -- never summed or overwritten ad hoc from
whatever a status poll happens to report.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.execution.base import FillEvent
from app.persistence.models import Execution, Order


def record_new_fills(session: Session, order: Order, fills: list[FillEvent]) -> list[Execution]:
    """Inserts only fills not already recorded for this order (deduped by
    `exchange_fill_id`), then recalculates `order.filled_qty`/
    `avg_fill_price`/`fees_total` from the FULL set of `Execution` rows now
    on file for it. Returns only the newly inserted rows -- the deltas a
    caller still needs to apply to a position; calling this again with the
    same fills (or a superset, as `poll_order()` always reports the full
    history) is always a safe no-op for anything already recorded."""
    if not fills:
        return []

    existing_ids = set(
        session.execute(
            select(Execution.exchange_fill_id).where(Execution.order_id == order.id)
        ).scalars().all()
    )
    existing_ids.discard(None)

    new_rows: list[Execution] = []
    for fill in fills:
        if fill.exchange_fill_id in existing_ids:
            continue
        row = Execution(
            order_id=order.id, exchange_fill_id=fill.exchange_fill_id,
            fill_qty=fill.fill_qty, fill_price=fill.fill_price, fee=fill.fee,
            is_partial=False, executed_at=utcnow(),
        )
        session.add(row)
        new_rows.append(row)
        existing_ids.add(fill.exchange_fill_id)

    if not new_rows:
        return []
    session.flush()

    all_rows = session.execute(
        select(Execution).where(Execution.order_id == order.id).order_by(Execution.id)
    ).scalars().all()
    total_qty = sum(r.fill_qty for r in all_rows)
    total_fee = sum(r.fee for r in all_rows)
    avg_price = (sum(r.fill_qty * r.fill_price for r in all_rows) / total_qty) if total_qty > 0 else 0.0

    order.filled_qty = total_qty
    order.avg_fill_price = avg_price
    order.fees_total = total_fee

    still_partial = total_qty < order.qty - 1e-9
    for row in new_rows:
        row.is_partial = still_partial
    session.flush()
    return new_rows
