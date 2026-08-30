from __future__ import annotations

import hashlib

from app.risk.engine import ApprovedOrder


def make_idempotency_key(order: ApprovedOrder, timestamp_bucket: str) -> str:
    """Deterministic key from signal id + symbol + side + a coarse time bucket
    (e.g. minute-resolution ISO string) so retries of the *same* decision
    collapse to one order, while a genuinely new signal gets a new key."""
    raw = f"{order.signal_id}:{order.symbol}:{order.side}:{order.qty:.8f}:{timestamp_bucket}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
