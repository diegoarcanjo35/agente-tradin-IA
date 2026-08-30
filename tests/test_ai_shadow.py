"""Covers spec section 7 items 18, 19, 25: invalid AI output, AI
unavailability (timeout), and the structural impossibility of the AI shadow
agent calling the Execution Engine.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.ai_shadow.guard import scan_ai_shadow_package
from app.ai_shadow.schemas import AIRecommendationOutput

AI_SHADOW_DIR = Path(__file__).resolve().parent.parent / "app" / "ai_shadow"


def test_simulated_provider_produces_valid_output():
    agent = AIShadowAgent(provider=SimulatedProvider(), timeout_seconds=2.0)
    result = agent.observe("BTCUSDT", {"fast_sma": 41000, "slow_sma": 40000})
    assert result.is_valid
    assert result.output.recommendation == "BUY"


def test_invalid_json_output_is_rejected_not_persisted_as_valid():
    class BrokenProvider:
        model_version = "broken-v1"
        name = "broken"

        def generate(self, symbol, market_context):
            return "not valid json {{{"

    agent = AIShadowAgent(provider=BrokenProvider(), timeout_seconds=2.0)
    result = agent.observe("BTCUSDT", {})
    assert not result.is_valid
    assert result.output is None
    assert result.rejection_reason is not None


def test_invalid_schema_missing_fields_is_rejected():
    class MissingFieldsProvider:
        model_version = "v1"
        name = "missing"

        def generate(self, symbol, market_context):
            return json.dumps({"symbol": "BTCUSDT", "recommendation": "BUY"})  # missing required fields

    agent = AIShadowAgent(provider=MissingFieldsProvider(), timeout_seconds=2.0)
    result = agent.observe("BTCUSDT", {})
    assert not result.is_valid


def test_bad_recommendation_enum_is_rejected():
    class BadEnumProvider:
        model_version = "v1"
        name = "bad_enum"

        def generate(self, symbol, market_context):
            return json.dumps({
                "symbol": "BTCUSDT", "recommendation": "STRONG_BUY", "confidence": 0.9,
                "reasoning_summary": "x", "risk_flags": [], "timestamp": "2024-01-01T00:00:00+00:00",
            })

    agent = AIShadowAgent(provider=BadEnumProvider(), timeout_seconds=2.0)
    result = agent.observe("BTCUSDT", {})
    assert not result.is_valid


def test_oversized_response_is_rejected():
    class HugeProvider:
        model_version = "v1"
        name = "huge"

        def generate(self, symbol, market_context):
            return json.dumps({
                "symbol": "BTCUSDT", "recommendation": "HOLD", "confidence": 0.1,
                "reasoning_summary": "x" * 10000, "risk_flags": [],
                "timestamp": "2024-01-01T00:00:00+00:00",
            })

    agent = AIShadowAgent(provider=HugeProvider(), timeout_seconds=2.0, max_response_chars=200)
    result = agent.observe("BTCUSDT", {})
    assert not result.is_valid
    assert "chars" in result.rejection_reason


def test_ai_unavailable_due_to_timeout_produces_no_valid_recommendation():
    class SlowProvider:
        model_version = "v1"
        name = "slow"

        def generate(self, symbol, market_context):
            time.sleep(1.0)
            return "{}"

    agent = AIShadowAgent(provider=SlowProvider(), timeout_seconds=0.05)
    result = agent.observe("BTCUSDT", {})
    assert not result.is_valid
    assert "timed out" in result.rejection_reason.lower()


def test_disabled_agent_produces_no_recommendation_at_all():
    agent = AIShadowAgent(provider=SimulatedProvider(), enabled=False)
    result = agent.observe("BTCUSDT", {})
    assert result is None


def test_ai_module_has_no_execution_import():
    """Structural proof: no file under app/ai_shadow imports app.execution,
    pybit, or references Bybit credential field names."""
    violations = scan_ai_shadow_package(AI_SHADOW_DIR)
    assert violations == {}, f"AI shadow module reaches execution/exchange: {violations}"


def test_ai_cannot_call_execution():
    """AIShadowAgent's public surface (observe) returns data only -- it never
    receives or exposes a reference to an ExecutionEngine or ApprovedOrder."""
    import inspect

    from app.ai_shadow.agent import AIShadowAgent as Agent

    sig = inspect.signature(Agent.observe)
    for param in sig.parameters.values():
        assert "execution" not in str(param.annotation).lower()
        assert "approvedorder" not in str(param.annotation).lower()
