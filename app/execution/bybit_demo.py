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
from app.core.errors import ExchangeDataIncompleteError, ExchangeTimeoutError, RateLimitError
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

# Correção v1.2 #1/#2: statuses that can carry a fill (including a residual
# fill on a cancellation that raced a partial fill -- "PartiallyFilledCanceled"
# maps to CANCELLED above) -- these are exactly the statuses that trigger a
# paginated /v5/execution/list walk before this engine will call the fill
# picture "complete" for that order.
_STATUSES_REQUIRING_FILL_SYNC = frozenset(
    {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED}
)

# Defensive caps -- Bybit's real V5 pagination caps `limit` at 100 per page;
# `_MAX_PAGES` is a backstop against an infinite loop from a
# misbehaving/malicious API response, never expected to be hit in practice.
_EXECUTION_PAGE_LIMIT = 50
_MAX_EXECUTION_PAGES = 50
_OPEN_ORDERS_PAGE_LIMIT = 50
_MAX_OPEN_ORDERS_PAGES = 50


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
        """ONE status query + (only for a status that can carry a fill) a
        FULLY PAGINATED `/v5/execution/list` walk -- no internal retry
        loop on the status query itself (the periodic poller IS the retry
        mechanism for that), but the execution-list pagination always runs
        to completion or reports itself incomplete via `fills_complete`.

        Correção v1.2 #1: a terminal `status` (FILLED/CANCELLED) is
        reported EVEN WHEN the fill sync is incomplete -- the caller
        (`app/execution/fill_service.py::apply_order_snapshot`) is the one
        that must never persist that terminal status until
        `fills_complete=True`. This engine's job is only to report the
        truth of both facts accurately, never to fabricate completeness."""
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
        fills_complete = True
        if status in _STATUSES_REQUIRING_FILL_SYNC:
            fills, fills_complete = self._fetch_all_executions(exchange_order_id)

        return OrderStatusSnapshot(
            exchange_order_id=exchange_order_id, status=status, fills=fills, fills_complete=fills_complete,
        )

    def _fetch_all_executions(self, exchange_order_id: str) -> tuple[list[FillEvent], bool]:
        """Correção v1.2 #2: walks every page of `/v5/execution/list` for
        this order via `nextPageCursor`, with an explicit page `limit`, a
        repeated-cursor guard, malformed-page detection, and a defensive
        page-count cap. Returns `(fills_gathered_so_far, complete)` --
        NEVER discards fills already validated before an interruption
        (timeout, rate limit, malformed page, repeated cursor, or the page
        cap), so a caller can safely record partial progress rather than
        lose it."""
        fills: list[FillEvent] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for _ in range(_MAX_EXECUTION_PAGES):
            params = {"category": "linear", "orderId": exchange_order_id, "limit": _EXECUTION_PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = self._http_get(f"{self.base_url}/v5/execution/list", params)
            except (ExchangeTimeoutError, RateLimitError) as exc:
                log_event(logger, 30, "execution_list_poll_failed", error=str(exc))
                return fills, False

            result = resp.get("result") or {}
            rows = result.get("list")
            if rows is None or not isinstance(rows, list):
                log_event(logger, 30, "execution_list_malformed_page")
                return fills, False

            try:
                for row in rows:
                    fills.append(FillEvent(
                        exchange_fill_id=row["execId"],
                        fill_qty=float(row["execQty"]),
                        fill_price=float(row["execPrice"]),
                        fee=float(row.get("execFee", 0.0)),
                    ))
            except (KeyError, TypeError, ValueError) as exc:
                log_event(logger, 30, "execution_list_malformed_row", error=str(exc))
                return fills, False

            next_cursor = result.get("nextPageCursor")
            if not next_cursor:
                return fills, True
            if next_cursor in seen_cursors:
                log_event(logger, 30, "execution_list_cursor_repeated")
                return fills, False
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        log_event(logger, 30, "execution_list_page_limit_exceeded")
        return fills, False

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
        """Correção v1.1 #3 / v1.2 #4: every order the exchange currently
        considers open (New/PartiallyFilled) for `symbol` -- used by
        reconciliation to detect an order the exchange has that isn't
        tracked locally.

        Correção v1.2 #4: a transport failure (timeout, rate limit) or a
        broken pagination contract (malformed page, repeated cursor, page
        cap exceeded) is NEVER swallowed into an empty list -- that made a
        reconciliation with no local open orders look "clean" even though
        the exchange was never actually reached. Every one of those cases
        now raises (`ExchangeTimeoutError`/`RateLimitError`/
        `ExchangeDataIncompleteError`), which `Orchestrator._reconcile_orders_step`
        already treats as a failed verification, never a valid empty
        result. Only a genuinely complete, successfully-paginated response
        may represent an empty list."""
        orders: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for _ in range(_MAX_OPEN_ORDERS_PAGES):
            params = {"category": "linear", "symbol": symbol, "limit": _OPEN_ORDERS_PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            resp = self._http_get(f"{self.base_url}/v5/order/realtime", params)

            result = resp.get("result") or {}
            rows = result.get("list")
            if rows is None or not isinstance(rows, list):
                raise ExchangeDataIncompleteError(
                    "Página malformada ao consultar ordens abertas na corretora."
                )
            orders.extend(
                {"exchange_order_id": row.get("orderId"), "side": row.get("side"), "qty": float(row.get("qty", 0.0))}
                for row in rows
                if row.get("orderStatus") in ("New", "PartiallyFilled")
            )

            next_cursor = result.get("nextPageCursor")
            if not next_cursor:
                return orders
            if next_cursor in seen_cursors:
                raise ExchangeDataIncompleteError(
                    "Cursor repetido ao paginar ordens abertas na corretora."
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise ExchangeDataIncompleteError(
            "Limite de páginas excedido ao paginar ordens abertas na corretora."
        )

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
            # Correção Stop/Take Pós-Preenchimento v1.1, Bloqueio 2: usado
            # pela reconciliação para detectar proteção remota divergente
            # da local -- Bybit devolve "" ou "0" quando não há stop/alvo
            # configurado, nunca omite o campo.
            "stop_loss": _parse_optional_protection_level(row.get("stopLoss")),
            "take_profit": _parse_optional_protection_level(row.get("takeProfit")),
        }

    def sync_position_protection(
        self, symbol: str, side: str, stop_loss: float | None, take_profit: float | None
    ) -> bool:
        """Correção Stop/Take Pós-Preenchimento v1.1, Bloqueio 2: `POST
        /v5/position/trading-stop`, `tpslMode=Full` (protege a posição
        inteira, ajustando a quantidade conforme o tamanho aberto --
        documentação oficial Bybit), `positionIdx=0` (compatível com o modo
        one-way já usado por este engine em `submit()`), ordens de proteção
        Market. Nunca levanta para uma falha de transporte esperada --
        retorna False, o chamador (`fill_service._sync_remote_protection`)
        decide a política de bloqueio/retry."""
        try:
            resp = self._http_post(
                f"{self.base_url}/v5/position/trading-stop",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "positionIdx": 0,
                    "tpslMode": "Full",
                    "stopLoss": f"{stop_loss:.2f}" if stop_loss is not None else "",
                    "takeProfit": f"{take_profit:.2f}" if take_profit is not None else "",
                    "slOrderType": "Market",
                    "tpOrderType": "Market",
                },
            )
        except (ExchangeTimeoutError, RateLimitError) as exc:
            log_event(logger, 40, "position_protection_sync_failed", error=str(exc))
            return False
        # Bybit V5: retCode 0 é sucesso; retCode 34040 ("not modified" -- os
        # níveis pedidos já são os vigentes) também conta como sincronizado.
        ret_code = resp.get("retCode")
        return ret_code in (0, 34040)


def _parse_optional_protection_level(raw) -> float | None:
    if raw in (None, "", "0"):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value != 0 else None
