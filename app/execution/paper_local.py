"""PAPER_LOCAL execution: simulates fills locally with configurable fee and
slippage, never contacts Bybit. Used for local development/testing of the
full pipeline without any exchange dependency.
"""
from __future__ import annotations

from typing import Callable

from app.core.logging import get_logger, log_event
from app.execution.base import FillResult
from app.risk.engine import ApprovedOrder

logger = get_logger(__name__)


class PaperLocalExecutionEngine:
    def __init__(
        self,
        price_provider: Callable[[str], float],
        fee_rate: float = 0.0006,
        slippage_bps: float = 5.0,
        partial_fill_ratio: float | None = None,
    ):
        """partial_fill_ratio, if set (0 < r < 1), simulates a partial fill for
        every order -- used by tests to exercise the partial-fill path."""
        self._price_provider = price_provider
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.partial_fill_ratio = partial_fill_ratio
        self._seen_keys: dict[str, FillResult] = {}
        self._positions: dict[str, dict] = {}

    def submit(self, order: ApprovedOrder, idempotency_key: str) -> FillResult:
        if idempotency_key in self._seen_keys:
            log_event(logger, 30, "duplicate_order_suppressed", idempotency_key=idempotency_key)
            return self._seen_keys[idempotency_key]

        price = self._price_provider(order.symbol)
        slip = price * (self.slippage_bps / 10_000.0)
        fill_price = price + slip if order.side == "BUY" else price - slip

        is_partial = self.partial_fill_ratio is not None
        fill_qty = order.qty * self.partial_fill_ratio if is_partial else order.qty
        fee = fill_qty * fill_price * self.fee_rate

        result = FillResult(
            exchange_order_id=f"PAPER-{idempotency_key[:16]}",
            fill_qty=fill_qty,
            fill_price=fill_price,
            fee=fee,
            is_partial=is_partial,
            status="PARTIALLY_FILLED" if is_partial else "FILLED",
        )
        self._seen_keys[idempotency_key] = result
        self._apply_to_position(order, result)
        return result

    def _apply_to_position(self, order: ApprovedOrder, result: FillResult) -> None:
        pos = self._positions.get(order.symbol)
        if pos is None:
            self._positions[order.symbol] = {
                "symbol": order.symbol,
                "side": order.side,
                "qty": result.fill_qty,
                "avg_entry_price": result.fill_price,
            }
        else:
            pos["qty"] += result.fill_qty

    def get_position(self, symbol: str) -> dict | None:
        return self._positions.get(symbol)
