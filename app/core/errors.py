"""Domain-specific exceptions. Raising one of these should always be safe-by-default:
callers that catch broadly must still end up blocking trading, never allowing it."""


class TradingSystemError(Exception):
    """Base class for all domain errors."""


class ProductionEndpointBlockedError(TradingSystemError):
    """Raised when configuration or a runtime request points at a non-demo/testnet host."""


class StaleDataError(TradingSystemError):
    """Raised when market data is older than the configured staleness threshold."""


class ClockDriftError(TradingSystemError):
    """Raised when local clock drift versus a trusted reference exceeds the safety threshold."""


class RiskRejectedError(TradingSystemError):
    """Raised (or represented as a rejection record) when the Risk Engine refuses a signal."""


class TradingBlockedError(TradingSystemError):
    """Raised when an action is attempted while the system is in TRADING_BLOCKED state."""


class DuplicateOrderError(TradingSystemError):
    """Raised when an idempotency key collision indicates a duplicate order submission."""


class ExchangeTimeoutError(TradingSystemError):
    """Raised when a call to the exchange (real or fake) exceeds its timeout."""


class RateLimitError(TradingSystemError):
    """Raised when the exchange reports a rate limit violation."""


class ReconciliationMismatchError(TradingSystemError):
    """Raised when local state disagrees with exchange-reported state after restart."""


class InvalidAIOutputError(TradingSystemError):
    """Raised when the AI shadow agent's output fails schema validation."""


class SecretLeakError(TradingSystemError):
    """Raised defensively if code path would emit a secret into logs or persistence."""
