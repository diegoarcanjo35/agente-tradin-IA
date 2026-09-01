from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.execution.order_state import OrderStatus
from app.risk.engine import ApprovedOrder


@dataclass(frozen=True)
class SubmitAck(object):
    """Correção v1.1 #1: `submit()` no longer blocks waiting for
    confirmation -- it returns immediately after the exchange accepts (or
    rejects) the order create request. `status` is only ever SUBMITTED
    (accepted, `exchange_order_id` is real), REJECTED (exchange refused
    outright), or UNKNOWN (couldn't even confirm acceptance -- e.g. a
    timeout on the create call itself). Confirmation of any actual fill
    always comes later, from a separate `poll_order()` call -- never from
    `submit()` itself."""
    exchange_order_id: str
    status: OrderStatus  # SUBMITTED | REJECTED | UNKNOWN


@dataclass(frozen=True)
class FillEvent:
    """One individual, idempotent fill -- `exchange_fill_id` is the
    exchange's own execution identifier (Bybit `execId`; for PAPER engines,
    a locally synthesized but still stable id). This is what makes fill
    application safe to repeat: the same `exchange_fill_id` is applied at
    most once, ever, regardless of how many times `poll_order()` reports
    it (see app/execution/fill_ledger.py)."""
    exchange_fill_id: str
    fill_qty: float
    fill_price: float
    fee: float


@dataclass(frozen=True)
class OrderStatusSnapshot:
    """Correção v1.1 #1/#2: result of `poll_order()` -- the exchange's
    current view of one order. `fills` is the FULL list of individual
    fills the exchange currently reports for this order (never just a
    delta) -- deduplication against what's already persisted is the
    ledger's job (app/execution/fill_ledger.py::record_new_fills), not the
    caller's, so a caller can safely re-poll and re-apply as often as it
    wants.

    Correção v1.2 #1/#2: `fills_complete` is True only when the engine
    proved it walked the exchange's full fill-history pagination for this
    order (every `nextPageCursor` followed to the end) -- False whenever a
    timeout, rate limit, malformed page, repeated cursor, or defensive
    page-count limit interrupted that walk partway. `fills` still carries
    whatever was validated before the interruption (never discarded), but
    `fills_complete=False` tells the caller (app/execution/fill_service.py)
    it must NOT treat a terminal `status` as safe to finalize yet -- see
    `app/execution/fill_service.py::apply_order_snapshot`."""
    exchange_order_id: str
    status: OrderStatus  # SUBMITTED | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED | UNKNOWN
    fills: list[FillEvent]
    fills_complete: bool = True


class ExecutionEngine(Protocol):
    """The only public entrypoint accepts an ApprovedOrder -- an order object
    that can only exist if app.risk.engine.RiskEngine approved it. No other
    order representation is accepted."""

    def submit(
        self, order: ApprovedOrder, idempotency_key: str, reference_price: float | None = None
    ) -> SubmitAck:
        """`reference_price`, when given, is the price of the candle (or the
        stop-loss/take-profit trigger price) that produced this decision --
        callers must always pass it rather than letting an engine fall back
        to a stale/default price. Engines backed by a real market (Bybit)
        ignore it since the exchange fill price is authoritative. Never
        confirms a fill -- see `SubmitAck`."""
        ...

    def poll_order(self, exchange_order_id: str) -> OrderStatusSnapshot:
        """Queries the current status and full fill history of one order.
        Called once immediately after every `submit()` that returns
        SUBMITTED (so the common case still resolves within the same
        tick), and again periodically by `Orchestrator._poll_open_orders`
        for any order still non-terminal -- this is what gives real
        acknowledgement-vs-confirmation separation and survives a process
        restart with an order in flight (correção v1.1 #1)."""
        ...

    def request_cancel(self, exchange_order_id: str) -> None:
        """Fire-and-forget cancellation request (correção v1.1 #1: the
        caller persists CANCEL_PENDING BEFORE calling this). Confirmation
        of the real outcome (CANCELLED, or a fill that won the race)
        always comes from a subsequent `poll_order()` call -- never
        assume success just because the request was accepted."""
        ...

    def list_open_orders(self, symbol: str) -> list[dict]:
        """Correção v1.1 #3: every order the EXCHANGE currently considers
        open for `symbol` -- `[{"exchange_order_id": ..., "side": ...,
        "qty": ...}, ...]`. Used by reconciliation to detect an order the
        exchange has that isn't tracked locally."""
        ...

    def get_position(self, symbol: str) -> dict | None:
        ...

    def sync_position_protection(
        self, symbol: str, side: str, stop_loss: float | None, take_profit: float | None
    ) -> bool:
        """Correção Stop/Take Pós-Preenchimento v1.1, Bloqueio 2: sincroniza
        a proteção (stop-loss/take-profit) da posição INTEIRA no lado
        remoto para os níveis fornecidos -- chamada pelo ponto único
        `app/execution/fill_service.py::_sync_remote_protection` depois de
        qualquer recálculo de níveis. PAPER_LOCAL/PAPER_LIVE nunca chamam
        endpoint algum (sempre retornam True -- nada remoto a sincronizar).
        BYBIT_DEMO é o único modo que realmente chama a corretora. Nunca
        levanta para uma falha de transporte esperada (timeout/rate limit)
        -- sempre retorna False nesse caso; o chamador decide a política de
        bloqueio/retry."""
        ...
