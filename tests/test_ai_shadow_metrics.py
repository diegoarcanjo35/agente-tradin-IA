"""Fase 2, item 7.10: AI Shadow agreement/counterfactual metrics -- pure
hindsight comparison, never a claim of independent AI authority or
performance.
"""
from __future__ import annotations

from app.metrics.ai_shadow_metrics import AIShadowObservation, compute_ai_shadow_metrics
from app.metrics.engine import UNAVAILABLE


def test_no_observations_reports_unavailable_not_zero():
    result = compute_ai_shadow_metrics([])
    assert result.total_observations == 0
    assert result.agreement_rate == UNAVAILABLE
    assert result.hypothetical_hit_rate == UNAVAILABLE
    assert result.counterfactual_pnl == UNAVAILABLE


def test_only_invalid_observations_reports_unavailable():
    observations = [
        AIShadowObservation(ai_recommendation="BUY", ai_is_valid=False, strategy_direction="BUY", realized_pnl=None),
    ]
    result = compute_ai_shadow_metrics(observations)
    assert result.total_observations == 1
    assert result.valid_observations == 0
    assert result.agreement_rate == UNAVAILABLE


def test_agreement_rate_only_counts_valid_observations():
    observations = [
        AIShadowObservation("BUY", True, "BUY", None),   # agree
        AIShadowObservation("SELL", True, "BUY", None),  # disagree
        AIShadowObservation("BUY", False, "SELL", None),  # invalid -- excluded from denominator
    ]
    result = compute_ai_shadow_metrics(observations)
    assert result.valid_observations == 2
    assert result.agreement_rate == 0.5


def test_hypothetical_hit_rate_only_over_agreed_and_closed_trades():
    observations = [
        AIShadowObservation("BUY", True, "BUY", realized_pnl=10.0),   # agree, win
        AIShadowObservation("BUY", True, "BUY", realized_pnl=-5.0),   # agree, loss
        AIShadowObservation("BUY", True, "BUY", realized_pnl=None),   # agree, never closed -- excluded
        AIShadowObservation("SELL", True, "BUY", realized_pnl=20.0),  # disagree -- excluded regardless of pnl
    ]
    result = compute_ai_shadow_metrics(observations)
    assert result.hypothetical_hit_rate == 0.5  # 1 win / 2 closed-and-agreed
    assert result.counterfactual_pnl == 5.0  # 10.0 + (-5.0)


def test_no_agreed_and_closed_trades_reports_unavailable_hit_rate():
    observations = [
        AIShadowObservation("BUY", True, "BUY", realized_pnl=None),  # agreed but never closed
        AIShadowObservation("SELL", True, "BUY", realized_pnl=10.0),  # closed but disagreed
    ]
    result = compute_ai_shadow_metrics(observations)
    assert result.hypothetical_hit_rate == UNAVAILABLE
    assert result.counterfactual_pnl == UNAVAILABLE
    assert result.agreement_rate == 0.5  # agreement itself is still computable
