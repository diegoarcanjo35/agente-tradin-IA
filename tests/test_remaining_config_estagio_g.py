"""Correção da Fase 2 v1.1 #5 (remanescente, Estágio G): PARTIAL_FILL_POLICY/
OPEN_ORDER_POLL_INTERVAL_SECONDS já estavam conectados desde o Estágio A/B --
os que faltavam eram PAPER_LIVE_FEE_RATE/PAPER_LIVE_SLIPPAGE_BPS (nunca
chegavam ao PaperLocalExecutionEngine em PAPER_LIVE, que usava seus próprios
defaults hardcoded) e o toggle externo do AI Shadow (nunca existia).
"""
from __future__ import annotations

from app.ai_shadow.agent import AIShadowAgent, SimulatedProvider
from app.ai_shadow.http_provider import HttpAIProvider
from app.api.main import build_orchestrator
from app.core.config import RunMode, Settings
from app.execution.paper_local import PaperLocalExecutionEngine
from tests.fakes.bybit_fake import FakeBybitTransport


def make_paper_live_settings(**overrides) -> Settings:
    defaults = dict(
        mode=RunMode.PAPER_LIVE,
        bybit_base_url="https://api-demo.bybit.com",
        bybit_ws_url="wss://stream-demo.bybit.com",
        database_url="sqlite:///:memory:",
        symbol="BTCUSDT",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# --- PAPER_LIVE fee/slippage genuinely wired --------------------------------

def test_paper_live_execution_engine_uses_the_configured_fee_rate_and_slippage():
    settings = make_paper_live_settings(paper_live_fee_rate=0.001234, paper_live_slippage_bps=42.0)
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    assert isinstance(orch.execution_engine, PaperLocalExecutionEngine)
    assert orch.execution_engine.fee_rate == 0.001234
    assert orch.execution_engine.slippage_bps == 42.0


def test_paper_live_default_fee_rate_and_slippage_match_settings_defaults():
    settings = make_paper_live_settings()
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    assert orch.execution_engine.fee_rate == settings.paper_live_fee_rate
    assert orch.execution_engine.slippage_bps == settings.paper_live_slippage_bps


def test_a_paper_live_fill_mathematically_reflects_the_configured_fee_rate():
    """Prova matemática: a fee registrada no fill é exatamente
    fee_rate * fill_qty * fill_price -- não um valor fixo/hardcoded."""
    settings = make_paper_live_settings(paper_live_fee_rate=0.002, paper_live_slippage_bps=0.0)
    transport = FakeBybitTransport()
    orch = build_orchestrator(settings, bybit_transport=transport)

    from tests.factories import approved_open_order

    approved = approved_open_order(
        symbol="BTCUSDT", side="BUY", qty=1.0, price=100.0,
        stop_loss=90.0, take_profit=110.0, signal_id=1,
    )
    ack = orch.execution_engine.submit(approved, idempotency_key="fee-proof-1", reference_price=100.0)
    snapshot = orch.execution_engine.poll_order(ack.exchange_order_id)

    assert len(snapshot.fills) == 1
    fill = snapshot.fills[0]
    expected_fee = 0.002 * fill.fill_qty * fill.fill_price
    assert abs(fill.fee - expected_fee) < 1e-9


# --- AI Shadow external provider toggle -------------------------------------

def test_ai_shadow_external_provider_disabled_by_default_uses_simulated_provider():
    from tests.test_price_correctness import build_test_orchestrator

    orch = build_orchestrator(Settings(mode=RunMode.REPLAY, database_url="sqlite:///:memory:"))
    assert isinstance(orch.ai_agent.provider, SimulatedProvider)


def test_ai_shadow_external_provider_enabled_without_key_or_url_stays_simulated():
    settings = Settings(
        mode=RunMode.REPLAY, database_url="sqlite:///:memory:",
        ai_shadow_external_provider_enabled=True,  # toggle on, but no key/url
    )
    orch = build_orchestrator(settings)
    assert isinstance(orch.ai_agent.provider, SimulatedProvider)


def test_ai_shadow_external_provider_enabled_and_fully_configured_swaps_the_provider():
    settings = Settings(
        mode=RunMode.REPLAY, database_url="sqlite:///:memory:",
        ai_shadow_external_provider_enabled=True,
        ai_provider_api_key="test-external-key",
        ai_provider_endpoint_url="https://example.invalid/ai",
    )
    orch = build_orchestrator(settings)
    assert isinstance(orch.ai_agent.provider, HttpAIProvider)


def test_http_ai_provider_posts_market_context_via_the_injected_transport():
    calls = []

    def fake_http_post(url, payload):
        calls.append((url, payload))
        return {"text": '{"symbol": "BTCUSDT", "recommendation": "HOLD", "confidence": 0.5, '
                         '"reasoning_summary": "teste", "risk_flags": [], "timestamp": "2024-01-01T00:00:00+00:00"}'}

    provider = HttpAIProvider(
        endpoint_url="https://example.invalid/ai", api_key="k", http_post=fake_http_post,
    )
    raw = provider.generate("BTCUSDT", {"fast_sma": 1.0, "slow_sma": 2.0})

    assert len(calls) == 1
    assert calls[0][0] == "https://example.invalid/ai"
    assert calls[0][1]["symbol"] == "BTCUSDT"
    assert calls[0][1]["api_key"] == "k"
    assert "HOLD" in raw


def test_http_ai_provider_works_end_to_end_through_ai_shadow_agent():
    def fake_http_post(url, payload):
        return {"text": '{"symbol": "BTCUSDT", "recommendation": "BUY", "confidence": 0.7, '
                         '"reasoning_summary": "teste externo", "risk_flags": [], '
                         '"timestamp": "2024-01-01T00:00:00+00:00"}'}

    provider = HttpAIProvider(
        endpoint_url="https://example.invalid/ai", api_key="k", http_post=fake_http_post,
    )
    agent = AIShadowAgent(provider=provider, enabled=True)
    result = agent.observe("BTCUSDT", {"fast_sma": 1.0, "slow_sma": 2.0})

    assert result.is_valid is True
    assert result.output.recommendation == "BUY"
    assert result.provider_name == "http_external"
