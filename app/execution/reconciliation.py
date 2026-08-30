"""Startup reconciliation: compares locally persisted OPEN positions against
what the exchange reports. Any disagreement -- or an inability to reach the
exchange at all -- results in TRADING_BLOCKED rather than a guess.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconciliationReport:
    ok: bool
    mismatches: list[str]


def reconcile_positions(local_positions: list[dict], remote_positions_by_symbol: dict[str, dict | None]) -> ReconciliationReport:
    mismatches: list[str] = []
    local_by_symbol = {p["symbol"]: p for p in local_positions}

    for symbol, local in local_by_symbol.items():
        remote = remote_positions_by_symbol.get(symbol)
        if remote is None:
            mismatches.append(f"{symbol}: posição local aberta, mas a corretora não reporta nenhuma (exchange reports none).")
            continue
        if abs(remote["qty"] - local["qty"]) > 1e-9:
            mismatches.append(
                f"{symbol}: divergência de quantidade (qty mismatch) local={local['qty']} corretora={remote['qty']}."
            )
        if remote["side"] != local["side"]:
            mismatches.append(
                f"{symbol}: divergência de lado (side mismatch) local={local['side']} corretora={remote['side']}."
            )

    for symbol, remote in remote_positions_by_symbol.items():
        if remote is not None and symbol not in local_by_symbol:
            mismatches.append(
                f"{symbol}: a corretora reporta uma posição aberta sem registro local (no local record)."
            )

    return ReconciliationReport(ok=not mismatches, mismatches=mismatches)
