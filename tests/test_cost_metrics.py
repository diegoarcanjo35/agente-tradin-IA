"""Fase 2, item 7.6: fees accumulated and realized slippage vs. the
reference price -- never a fabricated zero when reference prices are
unknown.
"""
from __future__ import annotations

from app.metrics.engine import UNAVAILABLE, OrderFillView, compute_cost_metrics


def test_empty_order_set_reports_zero_fees_and_unavailable_slippage():
    result = compute_cost_metrics([])
    assert result.fees_total == 0.0  # a genuine, correct total for nothing -- not fabricated
    assert result.slippage_avg_usd == UNAVAILABLE
    assert result.slippage_total_usd == UNAVAILABLE
    assert result.priced_orders_count == 0


def test_orders_without_any_reference_price_report_slippage_unavailable():
    orders = [
        OrderFillView(side="BUY", reference_price=None, avg_fill_price=100.0, fees_total=0.05),
        OrderFillView(side="SELL", reference_price=None, avg_fill_price=101.0, fees_total=0.06),
    ]
    result = compute_cost_metrics(orders)
    assert result.fees_total == 0.11
    assert result.slippage_avg_usd == UNAVAILABLE
    assert result.unpriced_orders_count == 2


def test_buy_slippage_is_positive_when_paying_more_than_reference():
    orders = [OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=100.5, fees_total=0.01)]
    result = compute_cost_metrics(orders)
    assert result.slippage_avg_usd == 0.5
    assert result.priced_orders_count == 1
    assert result.unpriced_orders_count == 0


def test_sell_slippage_is_positive_when_receiving_less_than_reference():
    orders = [OrderFillView(side="SELL", reference_price=100.0, avg_fill_price=99.5, fees_total=0.01)]
    result = compute_cost_metrics(orders)
    assert result.slippage_avg_usd == 0.5


def test_favorable_slippage_is_negative():
    """BUY filling BELOW reference, or SELL filling ABOVE reference, is
    favorable -- reported as negative, not clamped to zero (never hides
    real information from the operator)."""
    orders = [OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=99.0, fees_total=0.0)]
    result = compute_cost_metrics(orders)
    assert result.slippage_avg_usd == -1.0


def test_mixed_priced_and_unpriced_orders_average_only_the_priced_ones():
    orders = [
        OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=101.0, fees_total=0.0),
        OrderFillView(side="BUY", reference_price=None, avg_fill_price=200.0, fees_total=0.0),
    ]
    result = compute_cost_metrics(orders)
    assert result.slippage_avg_usd == 1.0  # only the priced order counted
    assert result.priced_orders_count == 1
    assert result.unpriced_orders_count == 1


def test_fees_total_always_sums_every_order_regardless_of_pricing():
    orders = [
        OrderFillView(side="BUY", reference_price=100.0, avg_fill_price=101.0, fees_total=0.02),
        OrderFillView(side="SELL", reference_price=None, avg_fill_price=99.0, fees_total=0.03),
    ]
    result = compute_cost_metrics(orders)
    assert result.fees_total == 0.05
