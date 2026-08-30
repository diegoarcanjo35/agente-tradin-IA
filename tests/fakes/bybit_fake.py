"""Fake Bybit HTTP transport. No network access -- used by tests that need to
exercise timeout, rate-limit, partial-fill, and duplicate-order paths without
ever calling a real exchange.

Correção v1.1 #1/#2/#3: extended with URL-based routing on `http_get` (it
used to only branch on `"orderId" in params`, regardless of URL) so a fake
can distinguish `/v5/order/realtime` (status) from `/v5/execution/list`
(individual fills) from a symbol-only open-orders query.

Correção v1.2 #1/#2/#3/#4: extended with a genuine, per-call PAGE QUEUE for
the three paginated endpoints (`queue_execution_pages`,
`queue_open_orders_pages`, `queue_funding_pages`) -- each queued item is
either a page dict (`{"list": [...], "nextPageCursor": "..."}`), an
exception instance to raise on that call (timeout/rate limit), or the
sentinel `"MALFORMED"` for a page missing its `list` key entirely. This is
what makes the adversarial pagination scenarios (multi-page, repeated
cursor, malformed page, mid-pagination failure) testable without any real
network. The pre-existing single-shot `queue_executions`/`set_open_orders`/
`set_funding_events` helpers are UNCHANGED and still work for every test
that doesn't care about pagination -- the page queue only takes over once
something has actually been queued into it for that key.
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
        self.execution_pages: dict[str, list] = {}  # order_id -> FIFO page queue (correção v1.2 #2)
        self.open_orders_pages: dict[str, list] = {}  # symbol -> FIFO page queue (correção v1.2 #4)
        self.funding_pages: list | None = None  # global FIFO page queue (correção v1.2 #3)
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
            if order_id in self.execution_pages:
                return self._pop_page(self.execution_pages, order_id)
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
            if symbol in self.open_orders_pages:
                return self._pop_page(self.open_orders_pages, symbol)
            return {"retCode": 0, "result": {"list": self.open_orders.get(symbol, [])}}
        if url.endswith("/v5/account/transaction-log"):
            if self.funding_pages is not None:
                return self._pop_page_from_list(self.funding_pages)
            return {"retCode": 0, "result": {"list": self.funding_events}}
        return {"retCode": 0, "result": {"list": []}}

    @staticmethod
    def _render_page(item) -> dict:
        if isinstance(item, Exception):
            raise item
        if item == "MALFORMED":
            return {"retCode": 0, "result": {}}  # no "list" key at all
        return {"retCode": 0, "result": item}

    def _pop_page(self, pages_dict: dict, key: str) -> dict:
        queue = pages_dict[key]
        if not queue:
            return {"retCode": 0, "result": {"list": []}}
        return self._render_page(queue.pop(0))

    def _pop_page_from_list(self, queue: list) -> dict:
        if not queue:
            return {"retCode": 0, "result": {"list": []}}
        return self._render_page(queue.pop(0))

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

    def queue_execution_pages(self, order_id: str, pages: list) -> None:
        """Correção v1.2 #2: a FIFO queue of raw `/v5/execution/list` pages
        for this order -- one item popped per call. Each item is a page
        dict `{"list": [...], "nextPageCursor": "..."}` (omit
        `nextPageCursor` on the final page), an exception instance
        (ExchangeTimeoutError/RateLimitError) to raise on that call, or the
        string `"MALFORMED"` for a response missing its `list` key
        entirely. Takes priority over `queue_executions` for this
        `order_id` once set (even to an empty list)."""
        self.execution_pages[order_id] = list(pages)

    def set_open_orders(self, symbol: str, rows: list[dict]) -> None:
        """Each row: {"orderId": ..., "orderStatus": "New"|"PartiallyFilled", "side": ..., "qty": ...}."""
        self.open_orders[symbol] = rows

    def queue_open_orders_pages(self, symbol: str, pages: list) -> None:
        """Correção v1.2 #4: same FIFO page-queue pattern as
        `queue_execution_pages`, for the symbol-only (no `orderId`)
        `/v5/order/realtime` query `list_open_orders` uses."""
        self.open_orders_pages[symbol] = list(pages)

    def set_funding_events(self, rows: list[dict]) -> None:
        """Each row: {"id": ..., "symbol": ..., "funding": ..., "transactionTime": ...}
        (Bybit's own transaction-log shape, type=SETTLEMENT -- `funding` is
        the actual funding amount; correção v1.2 #3 fixed a bug where
        `change`, the total account delta, was read instead)."""
        self.funding_events = rows

    def queue_funding_pages(self, pages: list) -> None:
        """Correção v1.2 #3: same FIFO page-queue pattern, for
        `/v5/account/transaction-log`. Global (not keyed by symbol/order)
        since a single BybitFundingProvider only ever queries one symbol
        per test."""
        self.funding_pages = list(pages)
