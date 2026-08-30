"""Covers spec section 7 items 12, 15, 24: stale data detection, Bybit
market-data rate limiting with backoff, and REPLAY mode never touching the
network (proved by construction: it takes only a local fixture path).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.core.errors import RateLimitError
from app.market_data.base import CandleFetchStatus
from app.market_data.bybit_provider import BybitDemoMarketDataProvider
from app.market_data.replay_provider import ReplayMarketDataProvider

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "replay_btcusdt.json"


def test_replay_provider_yields_deterministic_candles():
    provider = ReplayMarketDataProvider(FIXTURE, symbol="BTCUSDT")
    result = provider.next_candle()
    assert result.status == CandleFetchStatus.CANDLE_AVAILABLE
    assert result.candle.symbol == "BTCUSDT"
    assert result.candle.source == "replay"


def test_replay_provider_exhausts_and_reports_replay_finished():
    provider = ReplayMarketDataProvider(FIXTURE, symbol="BTCUSDT")
    count = 0
    while True:
        result = provider.next_candle()
        if result.status == CandleFetchStatus.REPLAY_FINISHED:
            break
        assert result.status == CandleFetchStatus.CANDLE_AVAILABLE
        count += 1
    assert count == len(provider)
    assert provider.next_candle().status == CandleFetchStatus.REPLAY_FINISHED


def test_replay_provider_module_has_no_network_imports():
    """REPLAY mode must be able to run with zero network access. Assert the
    module doesn't import requests/httpx/pybit/websockets/socket."""
    source_path = Path(inspect.getfile(ReplayMarketDataProvider))
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden = {"requests", "httpx", "pybit", "websockets", "socket", "aiohttp"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    assert not (found & forbidden), f"REPLAY provider imports network libs: {found & forbidden}"


def test_bybit_provider_is_stale_before_first_candle():
    def fake_get(url, params):
        return {"result": {"list": []}}

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1", http_get=fake_get, sleep=lambda s: None
    )
    assert provider.is_stale(max_staleness_seconds=30) is True


def test_bybit_provider_backs_off_on_rate_limit_and_reports_retryable_error():
    calls = {"n": 0}
    sleeps = []

    def fake_get(url, params):
        calls["n"] += 1
        raise RateLimitError("rate limited")

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1",
        http_get=fake_get, sleep=lambda s: sleeps.append(s),
    )
    result = provider.next_candle()
    assert result.status == CandleFetchStatus.RETRYABLE_ERROR
    assert calls["n"] == 1
    assert len(sleeps) == 1
    assert sleeps[0] > 0


def test_bybit_provider_empty_response_reports_no_new_candle():
    def fake_get(url, params):
        return {"result": {"list": []}}

    provider = BybitDemoMarketDataProvider(
        "https://api-demo.bybit.com", "BTCUSDT", "1", http_get=fake_get, sleep=lambda s: None
    )
    result = provider.next_candle()
    assert result.status == CandleFetchStatus.NO_NEW_CANDLE


def test_bybit_provider_rejects_non_demo_host():
    from app.core.errors import ProductionEndpointBlockedError

    with pytest.raises(ProductionEndpointBlockedError):
        BybitDemoMarketDataProvider(
            "https://api.bybit.com", "BTCUSDT", "1", http_get=lambda u, p: {}, sleep=lambda s: None
        )
