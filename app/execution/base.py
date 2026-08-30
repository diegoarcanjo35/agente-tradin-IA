from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.risk.engine import ApprovedOrder


@dataclass(frozen=True)
class FillResult:
    exchange_order_id: str
    fill_qty: float
    fill_price: float
    fee: float
    is_partial: bool
    status: str  # FILLED | PARTIALLY_FILLED | REJECTED | ERROR


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
