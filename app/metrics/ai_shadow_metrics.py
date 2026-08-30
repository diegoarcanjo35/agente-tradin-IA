"""Fase 2, item 7.10: AI Shadow agreement/counterfactual metrics.

Pure functions comparing what the AI Shadow recommended against what
actually happened after the strategy's OWN (authoritative) decision. The
AI never has execution authority (see app/ai_shadow/guard.py) -- these
metrics are pure hindsight analysis, and `hypothetical_hit_rate`/
`counterfactual_pnl` are explicitly labeled as simulation everywhere they
surface (dashboard, docs), never presented as a real track record.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.metrics.engine import UNAVAILABLE, Metric


@dataclass(frozen=True)
class AIShadowObservation:
    """One (recommendation, actual outcome) pair to compare -- built from a
    joined AIRecommendation + StrategySignal + eventual Position outcome,
    matched by signal_id."""

    ai_recommendation: str  # BUY | SELL | HOLD
    ai_is_valid: bool
    strategy_direction: str  # BUY | SELL | HOLD -- the strategy's own actual decision
    realized_pnl: float | None  # None if this signal never resulted in a closed trade


@dataclass(frozen=True)
class AIShadowMetricsResult:
    total_observations: int
    valid_observations: int
    agreement_rate: Metric  # fraction of VALID observations where the AI matched the strategy's direction
    hypothetical_hit_rate: Metric  # SIMULAÇÃO -- see module docstring
    counterfactual_pnl: Metric  # SIMULAÇÃO -- see module docstring


def compute_ai_shadow_metrics(observations: list[AIShadowObservation]) -> AIShadowMetricsResult:
    total = len(observations)
    valid = [o for o in observations if o.ai_is_valid]
    if not valid:
        return AIShadowMetricsResult(
            total_observations=total, valid_observations=0,
            agreement_rate=UNAVAILABLE, hypothetical_hit_rate=UNAVAILABLE, counterfactual_pnl=UNAVAILABLE,
        )

    agreements = [o for o in valid if o.ai_recommendation == o.strategy_direction]
    agreement_rate = len(agreements) / len(valid)

    # Hindsight-only: restricted to trades that actually closed (a known
    # realized_pnl) AND where the AI's recommendation matched the strategy's
    # real, authoritative decision. This is never a claim about what the AI
    # would have produced running independently -- only "when the AI agreed
    # with what actually happened, how did that turn out."
    agreed_closed = [o for o in agreements if o.realized_pnl is not None]
    if not agreed_closed:
        hypothetical_hit_rate: Metric = UNAVAILABLE
        counterfactual_pnl: Metric = UNAVAILABLE
    else:
        wins = [o for o in agreed_closed if o.realized_pnl > 0]
        hypothetical_hit_rate = len(wins) / len(agreed_closed)
        counterfactual_pnl = sum(o.realized_pnl for o in agreed_closed)

    return AIShadowMetricsResult(
        total_observations=total, valid_observations=len(valid),
        agreement_rate=agreement_rate,
        hypothetical_hit_rate=hypothetical_hit_rate,
        counterfactual_pnl=counterfactual_pnl,
    )
