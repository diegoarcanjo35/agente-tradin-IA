"""Reconciliation: compares locally persisted state against what the
exchange reports -- positions (Fase 1), and orders/fills (correção Fase 2
v1.1 #3). Any disagreement -- or an inability to reach the exchange at all
-- results in TRADING_BLOCKED rather than a guess. Never auto-repairs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Correção v1.1 #3: the auditor reproduced local qty=side=remote but
# avg_entry_price=100 vs. 999 going undetected -- price was never compared
# at all. A relative tolerance (not absolute) is used since price scale
# varies enormously across symbols; 0.1% comfortably exceeds normal
# floating-point/rounding noise while still catching a genuinely different
# price.
PRICE_RELATIVE_TOLERANCE = 0.001


@dataclass(frozen=True)
class ReconciliationReport:
    ok: bool
    mismatches: list[str]


def reconcile_positions(local_positions: list[dict], remote_positions_by_symbol: dict[str, dict | None]) -> ReconciliationReport:
    """`local`/`remote` position dicts: `{"symbol", "side", "qty",
    "avg_entry_price"}` -- `avg_entry_price` is optional on the remote side
    for backward compatibility with a caller that can't supply it (treated
    as "unknown, don't compare price" rather than a mismatch)."""
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
        local_price = local.get("avg_entry_price")
        remote_price = remote.get("avg_entry_price")
        if local_price is not None and remote_price is not None and local_price > 0:
            relative_diff = abs(remote_price - local_price) / local_price
            if relative_diff > PRICE_RELATIVE_TOLERANCE:
                mismatches.append(
                    f"{symbol}: divergência de preço médio (avg_entry_price mismatch) "
                    f"local={local_price} corretora={remote_price} "
                    f"(diferença relativa {relative_diff:.4%} > tolerância {PRICE_RELATIVE_TOLERANCE:.2%})."
                )

    for symbol, remote in remote_positions_by_symbol.items():
        if remote is not None and symbol not in local_by_symbol:
            mismatches.append(
                f"{symbol}: a corretora reporta uma posição aberta sem registro local (no local record)."
            )

    return ReconciliationReport(ok=not mismatches, mismatches=mismatches)


@dataclass(frozen=True)
class OrderReconciliationReport:
    ok: bool
    mismatches: list[str]
    unknown_remote_order_ids: list[str] = field(default_factory=list)


def reconcile_orders(
    local_open_orders: list[dict], remote_open_orders: list[dict],
) -> OrderReconciliationReport:
    """Correção v1.1 #3: `local_open_orders`/`remote_open_orders`:
    `[{"exchange_order_id", "side", "qty"}, ...]`. Detects an order the
    exchange has open that isn't tracked locally (`unknown_remote_order_ids`
    -- never auto-adopted, only reported) and a locally-tracked
    non-terminal order the exchange no longer knows about (a genuine
    divergence -- distinct from a fill/cancel simply not polled yet, which
    is the caller's responsibility to have already reconciled via
    poll_order() before calling this)."""
    mismatches: list[str] = []
    local_by_id = {o["exchange_order_id"]: o for o in local_open_orders if o.get("exchange_order_id")}
    remote_by_id = {o["exchange_order_id"]: o for o in remote_open_orders if o.get("exchange_order_id")}

    unknown_remote_order_ids: list[str] = []
    for order_id, remote in remote_by_id.items():
        if order_id not in local_by_id:
            unknown_remote_order_ids.append(order_id)
            mismatches.append(
                f"Ordem {order_id}: a corretora reporta como aberta, sem registro local não-terminal."
            )

    for order_id, local in local_by_id.items():
        remote = remote_by_id.get(order_id)
        if remote is None:
            mismatches.append(
                f"Ordem {order_id}: registrada localmente como não-terminal, mas a corretora não a "
                f"reporta mais como aberta (pode já ter sido preenchida/cancelada sem confirmação local)."
            )
            continue
        if abs(float(remote.get("qty", 0)) - float(local.get("qty", 0))) > 1e-9:
            mismatches.append(
                f"Ordem {order_id}: divergência de quantidade local={local.get('qty')} "
                f"corretora={remote.get('qty')}."
            )
        if remote.get("side") != local.get("side"):
            mismatches.append(
                f"Ordem {order_id}: divergência de lado local={local.get('side')} "
                f"corretora={remote.get('side')}."
            )

    return OrderReconciliationReport(
        ok=not mismatches, mismatches=mismatches, unknown_remote_order_ids=unknown_remote_order_ids,
    )
