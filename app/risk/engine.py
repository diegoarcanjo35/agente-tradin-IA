"""The Risk Engine has sole authority to approve an order. It is deterministic
code -- no ML, no strategy input beyond the structured Signal -- and every
check plus its outcome is recorded so a rejection or approval is always
explainable.

Structural enforcement of "no decision can bypass the Risk Engine": the
Execution Engine's public API only accepts an `ApprovedOrder`, and
`ApprovedOrder` can only be constructed with a `_RiskApprovalToken`, an opaque
sentinel that is never imported outside this module. See
tests/test_risk_engine.py::test_execution_requires_risk_approval and
tests/test_ai_shadow.py::test_ai_cannot_call_execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.risk.config import RiskLimits
from app.strategy.schemas import Signal


class _RiskApprovalToken:
    """Opaque sentinel. Do not import this outside app/risk/engine.py."""


@dataclass(frozen=True)
class ApprovedOrder:
    signal_id: int
    symbol: str
    side: str  # BUY | SELL
    qty: float
    stop_loss: float
    take_profit: float | None
    token: _RiskApprovalToken

    def __post_init__(self) -> None:
        if not isinstance(self.token, _RiskApprovalToken):
            raise TypeError(
                "ApprovedOrder can only be constructed via RiskEngine.evaluate() -- "
                "no other code path may mint an approval token."
            )


@dataclass(frozen=True)
class RiskContext:
    open_positions_count: int
    open_exposure_usd: float
    daily_realized_loss_usd: float
    consecutive_losses: int
    data_is_stale: bool
    api_failure_count: int
    clock_drift_seconds: float
    kill_switch_engaged: bool
    trading_blocked: bool
    cooldown_until: datetime | None
    now: datetime


@dataclass(frozen=True)
class RiskEvaluationResult:
    approved: bool
    reason: str
    checks: dict = field(default_factory=dict)
    approved_order: ApprovedOrder | None = None


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def evaluate(self, signal: Signal, signal_id: int, context: RiskContext) -> RiskEvaluationResult:
        checks: dict[str, bool] = {}
        limits = self.limits

        def reject(check_name: str, reason: str) -> RiskEvaluationResult:
            checks[check_name] = False
            return RiskEvaluationResult(approved=False, reason=reason, checks=checks)

        checks["kill_switch_engaged"] = not context.kill_switch_engaged
        if context.kill_switch_engaged:
            return reject("kill_switch_engaged", "Kill switch is engaged; all trading blocked.")

        checks["trading_blocked"] = not context.trading_blocked
        if context.trading_blocked:
            return reject("trading_blocked", "System state is TRADING_BLOCKED.")

        checks["data_fresh"] = not context.data_is_stale
        if context.data_is_stale:
            return reject("data_fresh", "Market data is stale; refusing to trade on stale data.")

        checks["clock_synced"] = abs(context.clock_drift_seconds) <= limits.max_clock_drift_seconds
        if not checks["clock_synced"]:
            return reject(
                "clock_synced",
                f"Clock drift {context.clock_drift_seconds:.2f}s exceeds "
                f"{limits.max_clock_drift_seconds}s.",
            )

        checks["api_failures_ok"] = context.api_failure_count < limits.max_api_failures
        if not checks["api_failures_ok"]:
            return reject(
                "api_failures_ok",
                f"API failure count {context.api_failure_count} reached limit "
                f"{limits.max_api_failures}.",
            )

        checks["cooldown_expired"] = (
            context.cooldown_until is None or context.now >= context.cooldown_until
        )
        if not checks["cooldown_expired"]:
            return reject(
                "cooldown_expired",
                f"Cooldown active after {context.consecutive_losses} consecutive losses "
                f"until {context.cooldown_until.isoformat()}.",
            )

        checks["actionable_signal"] = signal.direction in ("BUY", "SELL")
        if not checks["actionable_signal"]:
            return reject("actionable_signal", "Signal direction is HOLD; nothing to evaluate.")

        checks["stop_loss_present"] = (not limits.require_stop_loss) or signal.stop_loss is not None
        if not checks["stop_loss_present"]:
            return reject("stop_loss_present", "Order rejected: no stop-loss on signal.")

        checks["daily_loss_within_limit"] = context.daily_realized_loss_usd < limits.max_daily_loss_usd
        if not checks["daily_loss_within_limit"]:
            return reject(
                "daily_loss_within_limit",
                f"Daily realized loss {context.daily_realized_loss_usd:.2f} USD reached limit "
                f"{limits.max_daily_loss_usd} USD.",
            )

        checks["concurrent_positions_ok"] = (
            context.open_positions_count < limits.max_concurrent_positions
        )
        if not checks["concurrent_positions_ok"]:
            return reject(
                "concurrent_positions_ok",
                f"Open positions {context.open_positions_count} reached limit "
                f"{limits.max_concurrent_positions}.",
            )

        remaining_exposure = limits.max_total_exposure_usd - context.open_exposure_usd
        checks["exposure_room_available"] = remaining_exposure > 0
        if not checks["exposure_room_available"]:
            return reject(
                "exposure_room_available",
                f"Open exposure {context.open_exposure_usd:.2f} USD already at/over limit "
                f"{limits.max_total_exposure_usd} USD.",
            )

        position_usd = min(limits.max_position_usd, remaining_exposure)
        checks["position_size_positive"] = position_usd > 0
        if not checks["position_size_positive"]:
            return reject("position_size_positive", "Computed position size is not positive.")

        qty = position_usd / signal.observed_price
        approved_order = ApprovedOrder(
            signal_id=signal_id,
            symbol=signal.symbol,
            side=signal.direction,
            qty=qty,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            token=_RiskApprovalToken(),
        )
        return RiskEvaluationResult(
            approved=True,
            reason=f"Approved: position_usd={position_usd:.2f}, qty={qty:.8f}.",
            checks=checks,
            approved_order=approved_order,
        )
