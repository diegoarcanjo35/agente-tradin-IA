"""BYBIT_DEMO execution: the only path that can reach the real (demo)
exchange. `http_post`/`http_get` are injected so production wiring uses a real
client built from pybit against a host that already passed
app.core.config.assert_demo_host, while tests use tests/fakes/bybit_fake.py
with zero network access.

An HTTP 200 is never treated as "executed" -- submit() always follows up with
a status confirmation call before returning a FILLED/PARTIALLY_FILLED result.
"""
from __future__ import annotations

import time
from typing import Callable

from app.core.config import assert_demo_host
from app.core.errors import ExchangeTimeoutError, RateLimitError
from app.core.logging import get_logger, log_event
from app.execution.base import CancelResult, FillResult
from app.execution.order_state import OrderStatus
from app.risk.engine import ApprovedOrder

logger = get_logger(__name__)


class BybitDemoExecutionEngine:
    def __init__(
        self,
        base_url: str,
        http_post: Callable[[str, dict], dict],
        http_get: Callable[[str, dict], dict],
        max_status_polls: int = 5,
        poll_interval_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        assert_demo_host(base_url)
        self.base_url = base_url
        self._http_post = http_post
        self._http_get = http_get
        self.max_status_polls = max_status_polls
        self.poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self._seen_keys: dict[str, FillResult] = {}

    def submit(
        self, order: ApprovedOrder, idempotency_key: str, reference_price: float | None = None
    ) -> FillResult:
        # reference_price is intentionally unused: BYBIT_DEMO always fills at
        # whatever price the exchange actually reports, never a local guess.
        if idempotency_key in self._seen_keys:
            log_event(logger, 30, "duplicate_order_suppressed", idempotency_key=idempotency_key)
            return self._seen_keys[idempotency_key]

        try:
            create_resp = self._http_post(
                f"{self.base_url}/v5/order/create",
                {
                    "category": "linear",
                    "symbol": order.symbol,
                    "side": "Buy" if order.side == "BUY" else "Sell",
                    "orderType": "Market",
                    "qty": f"{order.qty:.8f}",
                    "stopLoss": f"{order.stop_loss:.2f}" if order.stop_loss is not None else None,
                    "takeProfit": f"{order.take_profit:.2f}" if order.take_profit else None,
                    "reduceOnly": order.is_close,
                    "orderLinkId": idempotency_key,
                },
            )
        except ExchangeTimeoutError as exc:
            log_event(logger, 40, "order_submit_timeout", error=str(exc))
            result = FillResult("", 0.0, 0.0, 0.0, False, OrderStatus.UNKNOWN)
            return result
        except RateLimitError as exc:
            log_event(logger, 40, "order_submit_rate_limited", error=str(exc))
            result = FillResult("", 0.0, 0.0, 0.0, False, OrderStatus.UNKNOWN)
            return result

        exchange_order_id = create_resp.get("result", {}).get("orderId")
        if not exchange_order_id:
            result = FillResult("", 0.0, 0.0, 0.0, False, OrderStatus.REJECTED)
            self._seen_keys[idempotency_key] = result
            return result

        # Never trust the create response alone: confirm via order status.
        result = self._confirm_status(order, exchange_order_id, idempotency_key)
        self._seen_keys[idempotency_key] = result
        return result

    def _confirm_status(self, order: ApprovedOrder, exchange_order_id: str, idempotency_key: str) -> FillResult:
        for attempt in range(self.max_status_polls):
            if attempt > 0:
                self._sleep(self.poll_interval_seconds)
            try:
                status_resp = self._http_get(
                    f"{self.base_url}/v5/order/realtime",
                    {"category": "linear", "orderId": exchange_order_id},
                )
            except (ExchangeTimeoutError, RateLimitError) as exc:
                log_event(logger, 30, "order_status_poll_failed", error=str(exc))
                continue

            rows = status_resp.get("result", {}).get("list", [])
            if not rows:
                continue
            row = rows[0]
            status = row.get("orderStatus")
            if status in ("Filled", "PartiallyFilled"):
                filled_qty = float(row.get("cumExecQty", 0.0))
                avg_price = float(row.get("avgPrice", 0.0))
                fee = float(row.get("cumExecFee", 0.0))
                return FillResult(
                    exchange_order_id=exchange_order_id,
                    fill_qty=filled_qty,
                    fill_price=avg_price,
                    fee=fee,
                    is_partial=(status == "PartiallyFilled"),
                    status=OrderStatus.FILLED if status == "Filled" else OrderStatus.PARTIALLY_FILLED,
                )
            if status in ("Rejected", "Cancelled"):
                return FillResult(exchange_order_id, 0.0, 0.0, 0.0, False, OrderStatus.REJECTED)
        # Could not confirm after polling: leave unresolved for reconciliation,
        # never claim FILLED without confirmation.
        return FillResult(exchange_order_id, 0.0, 0.0, 0.0, False, OrderStatus.UNKNOWN)

    def cancel(self, exchange_order_id: str) -> CancelResult:
        """Fase 2, item 7.3: requests cancellation, then ALWAYS confirms the
        real final state via the same status-polling loop as submit() --
        never assumes CANCELLED just because the cancel request was
        accepted. If a fill won the race before the cancel took effect,
        this reports FILLED/PARTIALLY_FILLED (with the real fill data)
        instead, so the caller persists that fill and adjusts the position
        rather than incorrectly treating the order as cancelled."""
        try:
            self._http_post(
                f"{self.base_url}/v5/order/cancel",
                {"category": "linear", "orderId": exchange_order_id},
            )
        except (ExchangeTimeoutError, RateLimitError) as exc:
            log_event(logger, 40, "order_cancel_request_failed", error=str(exc))
            # Fall through to confirmation regardless -- the cancel may have
            # been received by the exchange even if the response was lost.

        for attempt in range(self.max_status_polls):
            if attempt > 0:
                self._sleep(self.poll_interval_seconds)
            try:
                status_resp = self._http_get(
                    f"{self.base_url}/v5/order/realtime",
                    {"category": "linear", "orderId": exchange_order_id},
                )
            except (ExchangeTimeoutError, RateLimitError) as exc:
                log_event(logger, 30, "order_cancel_status_poll_failed", error=str(exc))
                continue

            rows = status_resp.get("result", {}).get("list", [])
            if not rows:
                continue
            row = rows[0]
            status = row.get("orderStatus")
            if status == "Cancelled":
                return CancelResult(exchange_order_id=exchange_order_id, status=OrderStatus.CANCELLED)
            if status in ("Filled", "PartiallyFilled"):
                # Race lost to a fill -- report it truthfully, never a fake cancel.
                return CancelResult(
                    exchange_order_id=exchange_order_id,
                    status=OrderStatus.FILLED if status == "Filled" else OrderStatus.PARTIALLY_FILLED,
                    filled_qty=float(row.get("cumExecQty", 0.0)),
                    avg_fill_price=float(row.get("avgPrice", 0.0)),
                    fee=float(row.get("cumExecFee", 0.0)),
                )
        # Could not confirm the final state -- never claim CANCELLED without confirmation.
        return CancelResult(exchange_order_id=exchange_order_id, status=OrderStatus.UNKNOWN)

    def get_position(self, symbol: str) -> dict | None:
        resp = self._http_get(
            f"{self.base_url}/v5/position/list", {"category": "linear", "symbol": symbol}
        )
        rows = resp.get("result", {}).get("list", [])
        if not rows or float(rows[0].get("size", 0)) == 0:
            return None
        row = rows[0]
        return {
            "symbol": symbol,
            "side": row.get("side"),
            "qty": float(row.get("size", 0)),
            "avg_entry_price": float(row.get("avgPrice", 0)),
        }
