"""BYBIT_DEMO execution: the only path that can reach the real (demo)
exchange. `http_post`/`http_get` are injected so production wiring uses a real
client built from pybit against a host that already passed
app.core.config.assert_demo_host, while tests use tests/fakes/bybit_fake.py
with zero network access.

Correção v1.1 #1: `submit()` no longer blocks waiting for confirmation --
it does ONLY the create call and returns SUBMITTED/REJECTED/UNKNOWN
immediately. An HTTP 200 on create is still never treated as "executed":
confirmation of any fill always comes from a separate, later `poll_order()`
call (invoked once immediately by the orchestrator, and again periodically
for any order still non-terminal -- see Orchestrator._poll_open_orders).
This engine itself no longer loops/retries internally; the periodic poller
IS the retry mechanism, which is also what makes "process restarted with an
order in flight" a real, recoverable case instead of a blocking wait.
"""
from __future__ import annotations

import time
from typing import Callable

from app.core.config import assert_demo_host
from app.core.errors import ExchangeTimeoutError, RateLimitError
from app.core.logging import get_logger, log_event
from app.execution.base import FillEvent, OrderStatusSnapshot, SubmitAck
from app.execution.order_state import OrderStatus
from app.risk.engine import ApprovedOrder

logger = get_logger(__name__)

# Bybit's real `orderStatus` values -> our OrderStatus. "New" (accepted, not
# yet filled) maps to SUBMITTED; anything not recognized maps to UNKNOWN
# rather than guessing.
_BYBIT_STATUS_MAP = {
    "New": OrderStatus.SUBMITTED,
    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "PartiallyFilledCanceled": OrderStatus.CANCELLED,
    "Rejected": OrderStatus.REJECTED,
}


class BybitDemoExecutionEngine:
    def __init__(
        self,
        base_url: str,
        http_post: Callable[[str, dict], dict],
        http_get: Callable[[str, dict], dict],
        sleep: Callable[[float], None] = time.sleep,
    ):
        assert_demo_host(base_url)
        self.base_url = base_url
        self._http_post = http_post
        self._http_get = http_get
        self._sleep = sleep

    def submit(
        self, order: ApprovedOrder, idempotency_key: str, reference_price: float | None = None
    ) -> SubmitAck:
        # reference_price is intentionally unused: BYBIT_DEMO always fills at
        # whatever price the exchange actually reports, never a local guess.
        # No idempotency cache here -- the caller (Orchestrator) never calls
        # submit() for an idempotency_key that already has a persisted
        # Order; the exchange's own orderLinkId dedup is a second layer.
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
        except (ExchangeTimeoutError, RateLimitError) as exc:
            log_event(logger, 40, "order_submit_failed", error=str(exc))
            return SubmitAck(exchange_order_id="", status=OrderStatus.UNKNOWN)

        exchange_order_id = create_resp.get("result", {}).get("orderId")
        if not exchange_order_id:
            return SubmitAck(exchange_order_id="", status=OrderStatus.REJECTED)
        return SubmitAck(exchange_order_id=exchange_order_id, status=OrderStatus.SUBMITTED)

    def poll_order(self, exchange_order_id: str) -> OrderStatusSnapshot:
        """ONE status query + (only if a fill is possible) ONE execution-
        list query -- no internal retry loop. Returns the FULL list of
        fills the exchange currently reports; the fill ledger
        (app/execution/fill_ledger.py) is what deduplicates against
        already-persisted `exchange_fill_id`s, so re-polling the same
        order repeatedly is always safe."""
        try:
            status_resp = self._http_get(
                f"{self.base_url}/v5/order/realtime",
                {"category": "linear", "orderId": exchange_order_id},
            )
        except (ExchangeTimeoutError, RateLimitError) as exc:
            log_event(logger, 30, "order_status_poll_failed", error=str(exc))
            return OrderStatusSnapshot(exchange_order_id=exchange_order_id, status=OrderStatus.UNKNOWN, fills=[])

        rows = status_resp.get("result", {}).get("list", [])
        if not rows:
            return OrderStatusSnapshot(exchange_order_id=exchange_order_id, status=OrderStatus.UNKNOWN, fills=[])

        raw_status = rows[0].get("orderStatus")
        status = _BYBIT_STATUS_MAP.get(raw_status, OrderStatus.UNKNOWN)

        fills: list[FillEvent] = []
        if status in (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED):
            try:
                exec_resp = self._http_get(
                    f"{self.base_url}/v5/execution/list",
                    {"category": "linear", "orderId": exchange_order_id},
                )
                for row in exec_resp.get("result", {}).get("list", []):
                    fills.append(FillEvent(
                        exchange_fill_id=row["execId"],
                        fill_qty=float(row.get("execQty", 0.0)),
                        fill_price=float(row.get("execPrice", 0.0)),
                        fee=float(row.get("execFee", 0.0)),
                    ))
            except (ExchangeTimeoutError, RateLimitError) as exc:
                # Status is still real and reportable; fill details are
                # simply deferred to the next poll rather than blocking or
                # fabricating them here.
                log_event(logger, 30, "execution_list_poll_failed", error=str(exc))

        return OrderStatusSnapshot(exchange_order_id=exchange_order_id, status=status, fills=fills)

    def request_cancel(self, exchange_order_id: str) -> None:
        """Fire-and-forget (correção v1.1 #1): the caller persists
        CANCEL_PENDING before calling this. Confirmation of the real
        outcome always comes from a subsequent `poll_order()` call."""
        try:
            self._http_post(
                f"{self.base_url}/v5/order/cancel",
                {"category": "linear", "orderId": exchange_order_id},
            )
        except (ExchangeTimeoutError, RateLimitError) as exc:
            log_event(logger, 40, "order_cancel_request_failed", error=str(exc))
            # The cancel may still have been received by the exchange even
            # if the response was lost -- the next poll_order() call is
            # what actually determines the truth, never this method.

    def list_open_orders(self, symbol: str) -> list[dict]:
        """Correção v1.1 #3: every order the exchange currently considers
        open (New/PartiallyFilled) for `symbol` -- used by reconciliation
        to detect an order the exchange has that isn't tracked locally."""
        try:
            resp = self._http_get(
                f"{self.base_url}/v5/order/realtime", {"category": "linear", "symbol": symbol},
            )
        except (ExchangeTimeoutError, RateLimitError) as exc:
            log_event(logger, 30, "list_open_orders_failed", error=str(exc))
            return []
        rows = resp.get("result", {}).get("list", [])
        return [
            {"exchange_order_id": row.get("orderId"), "side": row.get("side"), "qty": float(row.get("qty", 0.0))}
            for row in rows
            if row.get("orderStatus") in ("New", "PartiallyFilled")
        ]

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
