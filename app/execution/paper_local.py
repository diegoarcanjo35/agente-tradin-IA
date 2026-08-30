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
        """price_provider is a fallback used only when the caller does not
        pass an explicit `reference_price` to submit() -- the orchestrator
        always passes the price of the candle that produced the decision
        (or, for a stop-loss/take-profit close, the trigger price), so in
        normal operation price_provider is never actually consulted; it
        exists so ad-hoc/test callers don't have to supply a price for every
        call. partial_fill_ratio, if set (0 < r < 1), simulates a partial
        fill for every order -- used by tests to exercise that path."""
        self._price_provider = price_provider
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.partial_fill_ratio = partial_fill_ratio
        self._seen_keys: dict[str, FillResult] = {}
        self._positions: dict[str, dict] = {}

    def submit(
        self, order: ApprovedOrder, idempotency_key: str, reference_price: float | None = None
    ) -> FillResult:
        if idempotency_key in self._seen_keys:
            log_event(logger, 30, "duplicate_order_suppressed", idempotency_key=idempotency_key)
            return self._seen_keys[idempotency_key]

        price = reference_price if reference_price is not None else self._price_provider(order.symbol)
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
        """Four cases (Fase 1 correction 9):
        1. No existing position -> opens a new one.
        2. Existing position, same side -> increases qty, recomputes the
           weighted average entry price.
        3. Existing position, opposite side, fill qty < position qty ->
           reduces the existing position (side/avg price unchanged).
        4. Existing position, opposite side, fill qty >= position qty ->
           closes the existing position; any excess quantity opens a new
           position on the opposite side (flip), never simply summed onto
           the old one.
        """
        pos = self._positions.get(order.symbol)

        if pos is None:
            self._positions[order.symbol] = {
                "symbol": order.symbol,
                "side": order.side,
                "qty": result.fill_qty,
                "avg_entry_price": result.fill_price,
            }
            return

        if pos["side"] == order.side:
            total_qty = pos["qty"] + result.fill_qty
            pos["avg_entry_price"] = (
                pos["avg_entry_price"] * pos["qty"] + result.fill_price * result.fill_qty
            ) / total_qty
            pos["qty"] = total_qty
            return

        if result.fill_qty < pos["qty"] - 1e-12:
            pos["qty"] -= result.fill_qty
            return

        if abs(result.fill_qty - pos["qty"]) <= 1e-12:
            del self._positions[order.symbol]
            return

        excess_qty = result.fill_qty - pos["qty"]
        self._positions[order.symbol] = {
            "symbol": order.symbol,
            "side": order.side,
            "qty": excess_qty,
            "avg_entry_price": result.fill_price,
        }

    def get_position(self, symbol: str) -> dict | None:
        return self._positions.get(symbol)
