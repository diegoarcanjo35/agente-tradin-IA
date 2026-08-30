"""Covers spec section 7 item 17: reconciliation after restart -- local vs.
exchange-reported position state must agree, or the system reports mismatches
rather than silently trusting local state.
"""
from __future__ import annotations

from app.execution.reconciliation import reconcile_orders, reconcile_positions


def test_reconciliation_ok_when_states_match():
    local = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001}]
    remote = {"BTCUSDT": {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001}}
    report = reconcile_positions(local, remote)
    assert report.ok
    assert report.mismatches == []


def test_reconciliation_detects_qty_mismatch():
    local = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001}]
    remote = {"BTCUSDT": {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.002}}
    report = reconcile_positions(local, remote)
    assert not report.ok
    assert any("qty mismatch" in m for m in report.mismatches)


def test_reconciliation_detects_local_only_position():
    local = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001}]
    remote = {"BTCUSDT": None}
    report = reconcile_positions(local, remote)
    assert not report.ok
    assert any("exchange reports none" in m for m in report.mismatches)


def test_reconciliation_detects_remote_only_position():
    local: list[dict] = []
    remote = {"BTCUSDT": {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001}}
    report = reconcile_positions(local, remote)
    assert not report.ok
    assert any("no local record" in m for m in report.mismatches)


def test_reconciliation_detects_side_mismatch():
    local = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001}]
    remote = {"BTCUSDT": {"symbol": "BTCUSDT", "side": "SELL", "qty": 0.001}}
    report = reconcile_positions(local, remote)
    assert not report.ok
    assert any("side mismatch" in m for m in report.mismatches)


def test_reconciliation_detects_avg_price_divergence_even_with_matching_side_and_qty():
    """Correção v1.1 #3: the exact bug reported by the auditor -- local and
    remote agree on symbol/side/qty (100 vs. 999 previously went
    undetected because avg_entry_price was never compared at all)."""
    local = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001, "avg_entry_price": 100.0}]
    remote = {"BTCUSDT": {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001, "avg_entry_price": 999.0}}
    report = reconcile_positions(local, remote)
    assert not report.ok
    assert any("avg_entry_price mismatch" in m for m in report.mismatches)


def test_reconciliation_tolerates_negligible_avg_price_rounding_noise():
    local = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001, "avg_entry_price": 100.0}]
    remote = {"BTCUSDT": {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001, "avg_entry_price": 100.02}}
    report = reconcile_positions(local, remote)
    assert report.ok
    assert report.mismatches == []


def test_reconciliation_skips_price_comparison_when_remote_price_unknown():
    local = [{"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001, "avg_entry_price": 100.0}]
    remote = {"BTCUSDT": {"symbol": "BTCUSDT", "side": "BUY", "qty": 0.001}}
    report = reconcile_positions(local, remote)
    assert report.ok


def test_reconcile_orders_ok_when_local_and_remote_match():
    local = [{"exchange_order_id": "EX-1", "side": "BUY", "qty": 0.01}]
    remote = [{"exchange_order_id": "EX-1", "side": "BUY", "qty": 0.01}]
    report = reconcile_orders(local, remote)
    assert report.ok
    assert report.mismatches == []
    assert report.unknown_remote_order_ids == []


def test_reconcile_orders_detects_unknown_remote_order():
    """An order the exchange has open but that isn't tracked locally at
    all -- never auto-adopted, only reported."""
    report = reconcile_orders([], [{"exchange_order_id": "EX-GHOST", "side": "BUY", "qty": 0.01}])
    assert not report.ok
    assert report.unknown_remote_order_ids == ["EX-GHOST"]
    assert any("EX-GHOST" in m for m in report.mismatches)


def test_reconcile_orders_detects_locally_tracked_order_missing_remotely():
    report = reconcile_orders([{"exchange_order_id": "EX-2", "side": "BUY", "qty": 0.01}], [])
    assert not report.ok
    assert report.unknown_remote_order_ids == []
    assert any("EX-2" in m for m in report.mismatches)


def test_reconcile_orders_detects_qty_and_side_mismatch_for_the_same_order_id():
    local = [{"exchange_order_id": "EX-3", "side": "BUY", "qty": 0.01}]
    remote = [{"exchange_order_id": "EX-3", "side": "SELL", "qty": 0.02}]
    report = reconcile_orders(local, remote)
    assert not report.ok
    assert any("EX-3" in m and "quantidade" in m for m in report.mismatches)
    assert any("EX-3" in m and "lado" in m for m in report.mismatches)
