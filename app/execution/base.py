from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.execution.order_state import OrderStatus
from app.risk.engine import ApprovedOrder


@dataclass(frozen=True)
class FillResult:
    exchange_order_id: str
    fill_qty: float
    fill_price: float
    fee: float
    is_partial: bool
    status: OrderStatus  # FILLED | PARTIALLY_FILLED | REJECTED | UNKNOWN (never a bare string)


@dataclass(frozen=True)
class CancelResult:
    """Fase 2, item 7.3: result of ExecutionEngine.cancel(). `status` is
    almost always CANCELLED, but a real exchange can lose the cancel/fill
    race -- if a fill won before the cancel was processed, `status` reports
    FILLED/PARTIALLY_FILLED instead, with the fill fields populated, so the
    caller persists that fill and adjusts the position correctly instead of
    assuming a clean cancel."""
    exchange_order_id: str
    status: OrderStatus  # CANCELLED | FILLED | PARTIALLY_FILLED | UNKNOWN
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    fee: float = 0.0


class ExecutionEngine(Protocol):
    """The only public entrypoint accepts an ApprovedOrder -- an order object
    that can only exist if app.risk.engine.RiskEngine approved it. No other
    order representation is accepted."""

    def submit(
        self, order: ApprovedOrder, idempotency_key: str, reference_price: float | None = None
    ) -> FillResult:
        """`reference_price`, when given, is the price of the candle (or the
        stop-loss/take-profit trigger price) that produced this decision --
        callers must always pass it rather than letting an engine fall back
        to a stale/default price. Engines backed by a real market (Bybit)
        ignore it since the exchange fill price is authoritative."""
        ...

    def get_position(self, symbol: str) -> dict | None:
        ...

    def cancel(self, exchange_order_id: str) -> CancelResult:
        """Cancel a non-terminal order (Fase 2, item 7.3). Must always
        confirm the real final state on the exchange -- never assume
        cancellation succeeded just because the request was accepted."""
        ...
