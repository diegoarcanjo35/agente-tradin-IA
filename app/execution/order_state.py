"""Fase 2, item 7.2: explicit, persistent order status state machine.

Before this module, `orders.status` was a free-text `String(16)` set once
to `"PENDING"` at creation and never advanced anywhere in the codebase --
`FillResult.status` (a bare string) was the closest thing to a status value,
documented only by a code comment. This module gives the eight required
states a real type and an explicit, enforced transition table, so an order
can never silently "skip" states or move somewhere illegal.
"""
from __future__ import annotations

from enum import Enum


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


# Terminal states: once reached, an order never transitions again.
TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})

# Non-terminal statuses that still represent open exposure a kill switch or
# cancellation flow needs to act on.
NON_TERMINAL_STATUSES = frozenset(
    {OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED,
     OrderStatus.CANCEL_PENDING, OrderStatus.UNKNOWN}
)

# Explicit allowed-transition table. UNKNOWN is reachable from every
# non-terminal state (a confirmation call that can't determine the real
# state must never guess) and can itself resolve to any state once a later
# confirmation succeeds -- it is the only state that both accepts and
# leaves toward everything non-terminal-incompatible.
_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_SUBMIT: frozenset({
        OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.UNKNOWN,
        OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
    }),
    OrderStatus.SUBMITTED: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.UNKNOWN,
    }),
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED,  # another partial fill -- stays in the same state
        OrderStatus.FILLED, OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED,
        OrderStatus.UNKNOWN,
    }),
    OrderStatus.CANCEL_PENDING: frozenset({
        OrderStatus.CANCELLED, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED,
        OrderStatus.UNKNOWN,
    }),
    OrderStatus.UNKNOWN: frozenset({
        OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED, OrderStatus.REJECTED,
        OrderStatus.UNKNOWN,
    }),
    # Terminal states: no outgoing transitions at all.
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class IllegalOrderTransitionError(Exception):
    """Raised in Portuguese (this can surface to an operator/audit trail)
    when code attempts an order status transition that is not in the
    explicit allowed table."""


def validate_transition(current: OrderStatus, new: OrderStatus) -> None:
    """Raises IllegalOrderTransitionError unless `new` is a permitted
    successor of `current`. A same-state transition is only permitted where
    explicitly listed above (e.g. PARTIALLY_FILLED -> PARTIALLY_FILLED for a
    second partial fill) -- every other identical-state repeat is rejected
    to force callers to use the idempotent `record_fill`/reconciliation
    paths instead of blindly re-writing status."""
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if new not in allowed:
        raise IllegalOrderTransitionError(
            f"Transição de status de ordem não permitida: {current.value} -> {new.value}. "
            f"Transições permitidas a partir de {current.value}: "
            f"{sorted(s.value for s in allowed) or '(nenhuma -- estado terminal)'}."
        )


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL_STATUSES
