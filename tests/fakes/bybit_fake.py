"""Fake Bybit HTTP transport. No network access -- used by tests that need to
exercise timeout, rate-limit, partial-fill, and duplicate-order paths without
ever calling a real exchange.

Correção v1.1 #1/#2/#3: extended with URL-based routing on `http_get` (it
used to only branch on `"orderId" in params`, regardless of URL) so a fake
can distinguish `/v5/order/realtime` (status) from `/v5/execution/list`
(individual fills) from a symbol-only open-orders query.
"""
from __future__ import annotations

from app.core.errors import ExchangeTimeoutError, RateLimitError


class FakeBybitTransport:
    def __init__(self):
        self.fail_next_n_with_timeout = 0
        self.fail_next_n_with_rate_limit = 0
        self.orders: dict[str, dict] = {}
        self.order_status_sequence: dict[str, list[dict]] = {}
        self.execution_queue: dict[str, list[dict]] = {}
        self.open_orders: dict[str, list[dict]] = {}  # symbol -> rows (order/realtime, no orderId)
        self.funding_events: list[dict] = []  # transaction-log rows (correção v1.1 #6)
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []

    def http_post(self, url: str, payload: dict) -> dict:
        self.post_calls.append((url, payload))
        if self.fail_next_n_with_timeout > 0:
            self.fail_next_n_with_timeout -= 1
            raise ExchangeTimeoutError("simulated timeout on order create")
        if self.fail_next_n_with_rate_limit > 0:
            self.fail_next_n_with_rate_limit -= 1
            raise RateLimitError("simulated rate limit on order create")

        if url.endswith("/v5/order/cancel"):
            return {"retCode": 0, "result": {"orderId": payload.get("orderId")}}

        order_link_id = payload["orderLinkId"]
        order_id = f"EX-{order_link_id[:12]}"
        self.orders[order_id] = {"orderLinkId": order_link_id, "payload": payload}
        return {"retCode": 0, "result": {"orderId": order_id}}

    def http_get(self, url: str, params: dict) -> dict:
        self.get_calls.append((url, params))
        if url.endswith("/v5/execution/list"):
            order_id = params.get("orderId")
            rows = self.execution_queue.get(order_id, [])
            return {"retCode": 0, "result": {"list": rows}}
        if url.endswith("/v5/order/realtime"):
            if "orderId" in params:
                order_id = params["orderId"]
                sequence = self.order_status_sequence.get(order_id)
                if sequence:
                    row = sequence.pop(0)
                    return {"retCode": 0, "result": {"list": [row]}}
                return {"retCode": 0, "result": {"list": []}}
            # Symbol-only query -- open orders listing (list_open_orders).
            symbol = params.get("symbol")
            return {"retCode": 0, "result": {"list": self.open_orders.get(symbol, [])}}
        if url.endswith("/v5/account/transaction-log"):
            return {"retCode": 0, "result": {"list": self.funding_events}}
        return {"retCode": 0, "result": {"list": []}}

    def queue_status(self, order_id: str, rows: list[dict]) -> None:
        self.order_status_sequence[order_id] = rows

    def queue_executions(self, order_id: str, rows: list[dict]) -> None:
        """Each row: {"execId": ..., "execQty": ..., "execPrice": ..., "execFee": ...}.
        Unlike queue_status (popped one-per-call), this is served in FULL on
        every poll -- exactly like the real /v5/execution/list endpoint,
        which always returns the whole history, not a delta. Callers rely
        on the fill ledger's dedup-by-exchange_fill_id, not on this fake
        trimming the list itself."""
        self.execution_queue[order_id] = rows

    def set_open_orders(self, symbol: str, rows: list[dict]) -> None:
        """Each row: {"orderId": ..., "orderStatus": "New"|"PartiallyFilled", "side": ..., "qty": ...}."""
        self.open_orders[symbol] = rows

    def set_funding_events(self, rows: list[dict]) -> None:
        """Each row: {"id": ..., "symbol": ..., "change": ..., "transactionTime": ...}
        (Bybit's own transaction-log shape, type=SETTLEMENT)."""
        self.funding_events = rows
