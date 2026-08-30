"""Covers spec section 7 item 17: reconciliation after restart -- local vs.
exchange-reported position state must agree, or the system reports mismatches
rather than silently trusting local state.
"""
from __future__ import annotations

from app.execution.reconciliation import reconcile_positions


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
