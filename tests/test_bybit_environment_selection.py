"""Correction v1.2 #6: the pybit client mode must be DERIVED from the
validated host, never hardcoded to demo=True regardless of what base_url
says. Fase 1 supports Demo Trading only -- plain Testnet was removed from
the allowlist because pybit's demo+testnet combination resolves to a THIRD
host, and testnet-alone would silently reconnect to plain Testnet even if
base_url pointed elsewhere. See app/core/config.py and docs/OPERACAO_DEMO.md.
"""
from __future__ import annotations

import pytest

from app.core.config import (
    ALLOWED_BYBIT_HOSTS,
    BYBIT_HOST_ENVIRONMENTS,
    assert_consistent_bybit_environment,
    get_host_environment,
)
from app.core.errors import ProductionEndpointBlockedError


def test_demo_host_resolves_to_demo_environment():
    assert get_host_environment("https://api-demo.bybit.com") == "demo"
    assert get_host_environment("wss://stream-demo.bybit.com") == "demo"


def test_testnet_host_is_not_in_the_allowlist_this_phase():
    """Fase 1 supports Demo Trading only -- plain Testnet is deliberately
    absent from the allowlist (see module docstring for why)."""
    assert "api-testnet.bybit.com" not in ALLOWED_BYBIT_HOSTS
    assert "stream-testnet.bybit.com" not in ALLOWED_BYBIT_HOSTS
    with pytest.raises(ProductionEndpointBlockedError):
        get_host_environment("https://api-testnet.bybit.com")


def test_demo_demo_pair_is_consistent():
    env = assert_consistent_bybit_environment(
        "https://api-demo.bybit.com", "wss://stream-demo.bybit.com"
    )
    assert env == "demo"


def test_inconsistent_pair_is_rejected_before_any_client_is_built():
    """A base_url/ws_url pair pointing at different (or unsupported)
    environments must fail before any network object exists."""
    with pytest.raises(ProductionEndpointBlockedError):
        assert_consistent_bybit_environment(
            "https://api-demo.bybit.com", "wss://api.bybit.com"  # production ws, mismatched
        )


def test_unknown_host_stays_blocked():
    with pytest.raises(ProductionEndpointBlockedError):
        get_host_environment("https://evil.example.com")


def test_build_pybit_client_derives_demo_kwargs_from_host(monkeypatch):
    """Never hardcodes demo=True regardless of input -- constructs the
    client only after successfully deriving the (single supported) 'demo'
    environment from the validated hosts."""
    from app.execution import bybit_pybit_client

    captured = {}

    class FakeHTTP:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(bybit_pybit_client, "HTTP", FakeHTTP)

    client = bybit_pybit_client.build_pybit_client(
        "https://api-demo.bybit.com", "wss://stream-demo.bybit.com", "key", "secret"
    )
    assert captured["demo"] is True
    assert captured["testnet"] is False
    assert captured["api_key"] == "key"
    assert captured["api_secret"] == "secret"


def test_build_pybit_client_rejects_mismatched_environment(monkeypatch):
    from app.execution import bybit_pybit_client

    class FakeHTTP:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(bybit_pybit_client, "HTTP", FakeHTTP)

    with pytest.raises(ProductionEndpointBlockedError):
        bybit_pybit_client.build_pybit_client(
            "https://api-demo.bybit.com", "wss://api.bybit.com", "key", "secret"
        )
