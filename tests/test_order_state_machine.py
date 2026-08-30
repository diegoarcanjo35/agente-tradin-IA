"""Fase 2, item 7.2: explicit order status state machine.

`orders.status` used to be a free-text string set once at creation and
never advanced anywhere -- this proves the new `OrderStatus` enum and its
transition table enforce the exact eight states and reject illegal jumps.
"""
from __future__ import annotations

import pytest

from app.execution.order_state import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    IllegalOrderTransitionError,
    OrderStatus,
    is_terminal,
    validate_transition,
)


def test_all_eight_required_statuses_exist():
    names = {s.value for s in OrderStatus}
    assert names == {
        "PENDING_SUBMIT", "SUBMITTED", "PARTIALLY_FILLED", "FILLED",
        "CANCEL_PENDING", "CANCELLED", "REJECTED", "UNKNOWN",
    }


@pytest.mark.parametrize("current,new", [
    (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED),
    (OrderStatus.PENDING_SUBMIT, OrderStatus.REJECTED),
    (OrderStatus.SUBMITTED, OrderStatus.FILLED),
    (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED),
    (OrderStatus.SUBMITTED, OrderStatus.CANCEL_PENDING),
    (OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED),
    (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED),
    (OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED),
    (OrderStatus.CANCEL_PENDING, OrderStatus.FILLED),  # race: fill won before cancel confirmed
    (OrderStatus.UNKNOWN, OrderStatus.SUBMITTED),
    (OrderStatus.UNKNOWN, OrderStatus.CANCELLED),
])
def test_valid_transitions_are_accepted(current, new):
    validate_transition(current, new)  # must not raise


@pytest.mark.parametrize("current,new", [
    (OrderStatus.PENDING_SUBMIT, OrderStatus.CANCELLED),  # can't cancel before submission accepted
    (OrderStatus.FILLED, OrderStatus.SUBMITTED),  # terminal -> anything
    (OrderStatus.CANCELLED, OrderStatus.FILLED),  # terminal -> anything
    (OrderStatus.REJECTED, OrderStatus.SUBMITTED),  # terminal -> anything
    (OrderStatus.SUBMITTED, OrderStatus.PENDING_SUBMIT),  # backwards
    (OrderStatus.FILLED, OrderStatus.FILLED),  # terminal has no self-loop either
])
def test_illegal_transitions_are_rejected_in_portuguese(current, new):
    with pytest.raises(IllegalOrderTransitionError) as excinfo:
        validate_transition(current, new)
    assert "não permitida" in str(excinfo.value)
    assert current.value in str(excinfo.value)
    assert new.value in str(excinfo.value)


def test_terminal_and_non_terminal_partition_is_exhaustive_and_disjoint():
    assert TERMINAL_STATUSES | NON_TERMINAL_STATUSES == set(OrderStatus)
    assert TERMINAL_STATUSES & NON_TERMINAL_STATUSES == set()
    for s in TERMINAL_STATUSES:
        assert is_terminal(s)
    for s in NON_TERMINAL_STATUSES:
        assert not is_terminal(s)


def test_no_terminal_status_has_any_outgoing_transition():
    """A terminal order (UNKNOWN cannot free new exposure, but is itself
    NOT terminal since it can still resolve) must never transition again --
    a UNKNOWN order that later resolves to CANCELLED etc. is fine, but a
    genuinely terminal one (FILLED/CANCELLED/REJECTED) is done forever."""
    from app.execution.order_state import _ALLOWED_TRANSITIONS

    for status in TERMINAL_STATUSES:
        assert _ALLOWED_TRANSITIONS[status] == frozenset()
