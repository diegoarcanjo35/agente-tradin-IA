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

    def submit(self, order: ApprovedOrder, idempotency_key: str) -> FillResult:
        ...

    def get_position(self, symbol: str) -> dict | None:
        ...
