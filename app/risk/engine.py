"""The Risk Engine has sole authority to approve an order -- for both opening
new exposure (`evaluate`) and closing/reducing existing exposure
(`evaluate_close`). It is deterministic code -- no ML, no strategy input
beyond the structured Signal -- and every check plus its outcome is recorded
so a rejection or approval is always explainable.

Structural enforcement of "no decision can bypass the Risk Engine": the
Execution Engine's public API only accepts an `ApprovedOrder`, and
`ApprovedOrder` can only be constructed with a `_RiskApprovalToken`, an opaque
sentinel that is private to this module. `_RiskApprovalToken` must never be
imported or instantiated outside app/risk/engine.py -- see
tests/test_risk_engine_boundary.py, which fails the build if any other file
does so. Tests that need an ApprovedOrder must use
`RiskEngine.make_test_approved_order(...)` below, a controlled, clearly
test-only factory that still only exists inside this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.risk.config import RiskLimits
from app.strategy.schemas import Signal


class _RiskApprovalToken:
    """Opaque sentinel. Private to app/risk/engine.py -- never import this."""


@dataclass(frozen=True)
class ApprovedOrder:
    signal_id: int
    symbol: str
    side: str  # BUY | SELL
    qty: float
    stop_loss: float | None
    take_profit: float | None
    token: _RiskApprovalToken
    is_close: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.token, _RiskApprovalToken):
            raise TypeError(
                "ApprovedOrder can only be constructed via RiskEngine.evaluate()/"
                "evaluate_close() -- no other code path may mint an approval token."
            )


@dataclass(frozen=True)
class RiskContext:
    open_positions_count: int
    open_exposure_usd: float
    daily_realized_loss_usd: float
    consecutive_losses: int
    data_is_stale: bool
    api_failure_count: int
    clock_drift_seconds: float | None
    kill_switch_engaged: bool
    trading_blocked: bool
    state_ambiguous: bool
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

    # -- Shared gating checks used by both evaluate() and evaluate_close() ---

    def _check_common_gates(self, checks: dict, context: RiskContext) -> str | None:
        """Returns a (check_name, already recorded) rejection reason string,
        or None if every common gate passes. Mutates `checks` in place."""
        limits = self.limits

        checks["kill_switch_engaged"] = not context.kill_switch_engaged
        if context.kill_switch_engaged:
            return "Kill switch is engaged; all trading blocked."

        checks["trading_blocked"] = not context.trading_blocked
        if context.trading_blocked:
            return "System state is TRADING_BLOCKED."

        checks["state_not_ambiguous"] = not context.state_ambiguous
        if context.state_ambiguous:
            return "Local state is ambiguous relative to the exchange; reconciliation required."

        checks["data_fresh"] = not context.data_is_stale
        if context.data_is_stale:
            return "Market data is stale; refusing to trade on stale data."

        drift = context.clock_drift_seconds
        checks["clock_synced"] = drift is not None and abs(drift) <= limits.max_clock_drift_seconds
        if not checks["clock_synced"]:
            if drift is None:
                return "Clock drift could not be determined; refusing to trade."
            return f"Clock drift {drift:.2f}s exceeds {limits.max_clock_drift_seconds}s."

        checks["api_failures_ok"] = context.api_failure_count < limits.max_api_failures
        if not checks["api_failures_ok"]:
            return (
                f"API failure count {context.api_failure_count} reached limit "
                f"{limits.max_api_failures}."
            )

        return None

    def evaluate(self, signal: Signal, signal_id: int, context: RiskContext) -> RiskEvaluationResult:
        checks: dict[str, bool] = {}
        limits = self.limits

        def reject(check_name: str, reason: str) -> RiskEvaluationResult:
            checks[check_name] = False
            return RiskEvaluationResult(approved=False, reason=reason, checks=checks)

        common_rejection = self._check_common_gates(checks, context)
        if common_rejection is not None:
            return RiskEvaluationResult(approved=False, reason=common_rejection, checks=checks)

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
            is_close=False,
        )
        return RiskEvaluationResult(
            approved=True,
            reason=f"Approved: position_usd={position_usd:.2f}, qty={qty:.8f}.",
            checks=checks,
            approved_order=approved_order,
        )

    def evaluate_close(
        self,
        signal_id: int,
        symbol: str,
        close_side: str,
        qty: float,
        position_exists: bool,
        position_qty: float,
        position_side: str,
        context: RiskContext,
    ) -> RiskEvaluationResult:
        """Approve a position-reducing/closing order. Deliberately skips the
        limits that exist only to gate *new* exposure (daily loss cap,
        concurrent-position cap, total-exposure cap, stop-loss requirement):
        closing a position reduces risk, so it is never blocked by those. It
        still enforces every safety gate that applies regardless of
        direction (kill switch, TRADING_BLOCKED, ambiguous state, stale
        data, clock sync, API health) plus close-specific sanity checks.
        """
        checks: dict[str, bool] = {}

        def reject(check_name: str, reason: str) -> RiskEvaluationResult:
            checks[check_name] = False
            return RiskEvaluationResult(approved=False, reason=reason, checks=checks)

        common_rejection = self._check_common_gates(checks, context)
        if common_rejection is not None:
            return RiskEvaluationResult(approved=False, reason=common_rejection, checks=checks)

        checks["position_exists"] = position_exists
        if not position_exists:
            return reject("position_exists", "No open position to close for this symbol.")

        checks["close_side_valid"] = close_side in ("BUY", "SELL") and close_side != position_side
        if not checks["close_side_valid"]:
            return reject(
                "close_side_valid",
                f"Close side {close_side!r} must be BUY/SELL and opposite of position side "
                f"{position_side!r}.",
            )

        checks["qty_positive"] = qty > 0
        if not checks["qty_positive"]:
            return reject("qty_positive", "Close quantity must be positive.")

        checks["qty_within_position"] = qty <= position_qty + 1e-12
        if not checks["qty_within_position"]:
            return reject(
                "qty_within_position",
                f"Close quantity {qty} exceeds open position quantity {position_qty}.",
            )

        approved_order = ApprovedOrder(
            signal_id=signal_id,
            symbol=symbol,
            side=close_side,
            qty=qty,
            stop_loss=None,
            take_profit=None,
            token=_RiskApprovalToken(),
            is_close=True,
        )
        return RiskEvaluationResult(
            approved=True,
            reason=f"Close approved: qty={qty:.8f} side={close_side}.",
            checks=checks,
            approved_order=approved_order,
        )

    @staticmethod
    def attempt_construct_with_invalid_token_for_testing(bad_token: object) -> ApprovedOrder:
        """Test-only: proves ApprovedOrder rejects anything but a genuine
        _RiskApprovalToken -- exists so tests can verify the guard without
        importing ApprovedOrder or _RiskApprovalToken themselves. Always
        raises TypeError when `bad_token` isn't a real token."""
        return ApprovedOrder(
            signal_id=1, symbol="TEST", side="BUY", qty=0.001,
            stop_loss=None, take_profit=None, token=bad_token,  # type: ignore[arg-type]
        )

    @staticmethod
    def make_test_approved_order(
        signal_id: int,
        symbol: str,
        side: str,
        qty: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        is_close: bool = False,
    ) -> ApprovedOrder:
        """Test-only factory. Production code must always go through
        evaluate() or evaluate_close() -- this exists so tests never need to
        import or instantiate the private _RiskApprovalToken themselves."""
        return ApprovedOrder(
            signal_id=signal_id, symbol=symbol, side=side, qty=qty,
            stop_loss=stop_loss, take_profit=take_profit,
            token=_RiskApprovalToken(), is_close=is_close,
        )
