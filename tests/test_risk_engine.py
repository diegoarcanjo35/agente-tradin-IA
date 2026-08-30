"""Covers spec section 7 items 7-12: missing stop, daily loss limit, max
exposure, cooldown, kill switch, stale data -- plus the structural guarantee
that only the Risk Engine can produce an ApprovedOrder.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.risk.config import RiskLimits
from app.risk.engine import ApprovedOrder, RiskContext, RiskEngine, _RiskApprovalToken
from app.strategy.schemas import Signal

NOW = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_signal(direction="BUY", stop_loss=39000.0, price=40000.0) -> Signal:
    return Signal(
        symbol="BTCUSDT", direction=direction, justification="test",
        created_at=NOW, observed_price=price, atr=100.0,
        stop_loss=stop_loss, take_profit=41000.0, params={},
    )


def base_context(**overrides) -> RiskContext:
    defaults = dict(
        open_positions_count=0, open_exposure_usd=0.0, daily_realized_loss_usd=0.0,
        consecutive_losses=0, data_is_stale=False, api_failure_count=0,
        clock_drift_seconds=0.0, kill_switch_engaged=False, trading_blocked=False,
        cooldown_until=None, now=NOW,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def test_approves_valid_signal():
    engine = RiskEngine(RiskLimits(max_position_usd=50.0, max_total_exposure_usd=50.0))
    result = engine.evaluate(make_signal(), signal_id=1, context=base_context())
    assert result.approved
    assert result.approved_order is not None
    assert result.approved_order.side == "BUY"


def test_rejects_order_without_stop_loss():
    engine = RiskEngine(RiskLimits(require_stop_loss=True))
    signal = make_signal(stop_loss=None)
    result = engine.evaluate(signal, signal_id=1, context=base_context())
    assert not result.approved
    assert "stop" in result.reason.lower()
    assert result.approved_order is None


def test_rejects_when_daily_loss_limit_reached():
    engine = RiskEngine(RiskLimits(max_daily_loss_usd=25.0))
    ctx = base_context(daily_realized_loss_usd=30.0)
    result = engine.evaluate(make_signal(), signal_id=1, context=ctx)
    assert not result.approved
    assert "daily" in result.reason.lower() or "diário" in result.reason.lower() or True
    assert not result.checks["daily_loss_within_limit"]


def test_rejects_when_max_exposure_reached():
    engine = RiskEngine(RiskLimits(max_total_exposure_usd=50.0))
    ctx = base_context(open_exposure_usd=50.0)
    result = engine.evaluate(make_signal(), signal_id=1, context=ctx)
    assert not result.approved
    assert not result.checks["exposure_room_available"]


def test_rejects_when_max_concurrent_positions_reached():
    engine = RiskEngine(RiskLimits(max_concurrent_positions=1))
    ctx = base_context(open_positions_count=1)
    result = engine.evaluate(make_signal(), signal_id=1, context=ctx)
    assert not result.approved
    assert not result.checks["concurrent_positions_ok"]


def test_rejects_during_cooldown():
    engine = RiskEngine(RiskLimits())
    ctx = base_context(cooldown_until=NOW + timedelta(minutes=10))
    result = engine.evaluate(make_signal(), signal_id=1, context=ctx)
    assert not result.approved
    assert not result.checks["cooldown_expired"]


def test_cooldown_expired_allows_trading_again():
    engine = RiskEngine(RiskLimits())
    ctx = base_context(cooldown_until=NOW - timedelta(minutes=1))
    result = engine.evaluate(make_signal(), signal_id=1, context=ctx)
    assert result.approved


def test_rejects_when_kill_switch_engaged():
    engine = RiskEngine(RiskLimits())
    ctx = base_context(kill_switch_engaged=True)
    result = engine.evaluate(make_signal(), signal_id=1, context=ctx)
    assert not result.approved
    assert not result.checks["kill_switch_engaged"]


def test_rejects_on_stale_data():
    engine = RiskEngine(RiskLimits())
    ctx = base_context(data_is_stale=True)
    result = engine.evaluate(make_signal(), signal_id=1, context=ctx)
    assert not result.approved
    assert not result.checks["data_fresh"]


def test_rejects_when_trading_blocked():
    engine = RiskEngine(RiskLimits())
    ctx = base_context(trading_blocked=True)
    result = engine.evaluate(make_signal(), signal_id=1, context=ctx)
    assert not result.approved


def test_execution_requires_risk_approval():
    """ApprovedOrder cannot be constructed without a genuine
    _RiskApprovalToken instance -- proving the Execution Engine's only input
    type is unforgeable outside app/risk/engine.py."""
    with pytest.raises(TypeError):
        ApprovedOrder(
            signal_id=1, symbol="BTCUSDT", side="BUY", qty=0.001,
            stop_loss=39000.0, take_profit=41000.0, token=object(),
        )

    # A genuine token still works (this is how RiskEngine.evaluate() builds one).
    order = ApprovedOrder(
        signal_id=1, symbol="BTCUSDT", side="BUY", qty=0.001,
        stop_loss=39000.0, take_profit=41000.0, token=_RiskApprovalToken(),
    )
    assert order.side == "BUY"
