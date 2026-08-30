"""Fake Bybit HTTP transport. No network access -- used by tests that need to
exercise timeout, rate-limit, partial-fill, and duplicate-order paths without
ever calling a real exchange.
"""
from __future__ import annotations

from app.core.errors import ExchangeTimeoutError, RateLimitError


class FakeBybitTransport:
    def __init__(self):
        self.fail_next_n_with_timeout = 0
        self.fail_next_n_with_rate_limit = 0
        self.orders: dict[str, dict] = {}
        self.order_status_sequence: dict[str, list[dict]] = {}
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

        order_link_id = payload["orderLinkId"]
        order_id = f"EX-{order_link_id[:12]}"
        self.orders[order_id] = {"orderLinkId": order_link_id, "payload": payload}
        return {"retCode": 0, "result": {"orderId": order_id}}

    def http_get(self, url: str, params: dict) -> dict:
        self.get_calls.append((url, params))
        if "orderId" in params:
            order_id = params["orderId"]
            sequence = self.order_status_sequence.get(order_id)
            if sequence:
                row = sequence.pop(0)
                return {"retCode": 0, "result": {"list": [row]}}
            return {"retCode": 0, "result": {"list": []}}
        return {"retCode": 0, "result": {"list": []}}

    def queue_status(self, order_id: str, rows: list[dict]) -> None:
        self.order_status_sequence[order_id] = rows
