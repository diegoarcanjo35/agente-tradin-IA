"""AI Shadow Agent: observation-only. It receives the same market data the
strategy sees, produces a validated structured recommendation, and is stored
for later comparison against the deterministic strategy. It has zero access
to Bybit credentials and zero ability to reach the Execution Engine -- this
module intentionally does not import anything from app.execution or
app.core.config's Bybit fields. See app/ai_shadow/guard.py and
tests/test_ai_shadow.py for the enforcement of that boundary.
"""
from __future__ import annotations

import concurrent.futures
import json
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from app.ai_shadow.schemas import AIRecommendationOutput
from app.core.clock import utcnow
from app.core.errors import InvalidAIOutputError
from app.core.logging import get_logger, log_event

logger = get_logger(__name__)


class AIProvider(Protocol):
    model_version: str

    def generate(self, symbol: str, market_context: dict) -> str:
        """Return raw text the caller will attempt to parse as JSON matching
        AIRecommendationOutput. May raise on internal failure."""
        ...


class SimulatedProvider:
    """Deterministic, fully offline provider. This is the default provider
    whenever no external AI API key is configured, so the shadow panel is
    always populated without requiring any credentials or network access."""

    model_version = "simulated-v1"
    name = "simulated"

    def generate(self, symbol: str, market_context: dict) -> str:
        fast = market_context.get("fast_sma")
        slow = market_context.get("slow_sma")
        if fast is not None and slow is not None:
            if fast > slow:
                rec, confidence = "BUY", 0.6
            elif fast < slow:
                rec, confidence = "SELL", 0.6
            else:
                rec, confidence = "HOLD", 0.5
        else:
            rec, confidence = "HOLD", 0.3

        payload = {
            "symbol": symbol,
            "recommendation": rec,
            "confidence": confidence,
            "reasoning_summary": (
                f"SimulatedProvider comparing fast/slow SMA for {symbol}; "
                f"no external model consulted."
            ),
            "risk_flags": [],
            "timestamp": utcnow().isoformat(),
        }
        return json.dumps(payload)


@dataclass(frozen=True)
class AIShadowResult:
    is_valid: bool
    output: AIRecommendationOutput | None
    rejection_reason: str | None
    provider_name: str
    model_version: str


class AIShadowAgent:
    def __init__(
        self,
        provider: AIProvider,
        timeout_seconds: float = 8.0,
        max_response_chars: int = 4000,
        enabled: bool = True,
    ):
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.max_response_chars = max_response_chars
        self.enabled = enabled

    def observe(self, symbol: str, market_context: dict) -> AIShadowResult | None:
        """Returns None when the shadow agent is disabled (no recommendation
        row should be written in that case)."""
        if not self.enabled:
            return None

        provider_name = getattr(self.provider, "name", type(self.provider).__name__)
        model_version = getattr(self.provider, "model_version", "unknown")

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.provider.generate, symbol, market_context)
                raw = future.result(timeout=self.timeout_seconds)
        except concurrent.futures.TimeoutError:
            log_event(logger, 40, "ai_shadow_timeout", symbol=symbol, timeout=self.timeout_seconds)
            return AIShadowResult(False, None, "Provider timed out.", provider_name, model_version)
        except Exception as exc:  # noqa: BLE001 - provider failures must not crash the app
            log_event(logger, 40, "ai_shadow_provider_error", symbol=symbol, error=str(exc))
            return AIShadowResult(False, None, f"Provider raised: {exc}", provider_name, model_version)

        if len(raw) > self.max_response_chars:
            log_event(logger, 30, "ai_shadow_oversized_response", symbol=symbol, length=len(raw))
            return AIShadowResult(
                False, None, f"Response exceeded {self.max_response_chars} chars.",
                provider_name, model_version,
            )

        try:
            data = json.loads(raw)
            output = AIRecommendationOutput.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            log_event(logger, 30, "ai_shadow_invalid_output", symbol=symbol, error=str(exc))
            return AIShadowResult(False, None, f"Invalid output schema: {exc}", provider_name, model_version)

        return AIShadowResult(True, output, None, provider_name, model_version)
