"""Covers spec section 7 items 20-22: refusing a production endpoint,
refusing to start BYBIT_DEMO without credentials, and redacting secrets
before they ever reach a log line.
"""
from __future__ import annotations

import os

import pytest

from app.core.config import ALLOWED_BYBIT_HOSTS, Settings, assert_demo_host
from app.core.errors import ProductionEndpointBlockedError
from app.core.logging import redact


def test_assert_demo_host_accepts_allowlisted_host():
    for host in ALLOWED_BYBIT_HOSTS:
        assert_demo_host(f"https://{host}")  # must not raise


def test_assert_demo_host_rejects_known_production_host():
    with pytest.raises(ProductionEndpointBlockedError):
        assert_demo_host("https://api.bybit.com")


def test_assert_demo_host_rejects_arbitrary_unknown_host():
    with pytest.raises(ProductionEndpointBlockedError):
        assert_demo_host("https://evil.example.com")


def test_settings_rejects_production_base_url_at_construction():
    with pytest.raises(ProductionEndpointBlockedError):
        Settings(bybit_base_url="https://api.bybit.com")


def test_bybit_demo_mode_requires_credentials():
    settings = Settings(mode="BYBIT_DEMO", bybit_api_key="", bybit_api_secret="")
    with pytest.raises(ProductionEndpointBlockedError):
        settings.require_bybit_credentials()


def test_bybit_demo_mode_with_credentials_passes():
    settings = Settings(mode="BYBIT_DEMO", bybit_api_key="k", bybit_api_secret="s")
    settings.require_bybit_credentials()  # must not raise


def test_redact_hides_secret_like_fields():
    payload = {
        "bybit_api_key": "SUPER-SECRET",
        "bybit_api_secret": "ALSO-SECRET",
        "password": "hunter2",
        "signature": "abcdef",
        "symbol": "BTCUSDT",
        "nested": {"api_key": "nested-secret", "qty": 1.0},
    }
    out = redact(payload)
    assert out["bybit_api_key"] == "***REDACTED***"
    assert out["bybit_api_secret"] == "***REDACTED***"
    assert out["password"] == "***REDACTED***"
    assert out["signature"] == "***REDACTED***"
    assert out["symbol"] == "BTCUSDT"
    assert out["nested"]["api_key"] == "***REDACTED***"
    assert out["nested"]["qty"] == 1.0


def test_default_settings_bind_host_is_local():
    """Correction v1.1 #10: the control API (kill switch, etc.) has no
    authentication in this phase, so the default configuration must stay
    bound to localhost."""
    settings = Settings()
    assert settings.api_host == "127.0.0.1"
    settings.assert_safe_bind_host()  # must not raise


def test_external_bind_host_is_refused_without_explicit_opt_in():
    from app.core.config import UnsafeBindHostError

    settings = Settings(api_host="0.0.0.0")
    with pytest.raises(UnsafeBindHostError):
        settings.assert_safe_bind_host()


def test_external_bind_host_allowed_with_explicit_opt_in():
    settings = Settings(api_host="0.0.0.0", api_allow_external_bind=True)
    settings.assert_safe_bind_host()  # must not raise


def test_env_example_has_no_real_secret_values():
    env_example_path = os.path.join(
        os.path.dirname(__file__), "..", ".env.example"
    )
    with open(env_example_path, "r", encoding="utf-8") as f:
        content = f.read()
    for line in content.splitlines():
        if line.startswith("BYBIT_API_KEY=") or line.startswith("BYBIT_API_SECRET=") \
                or line.startswith("AI_PROVIDER_API_KEY="):
            _, _, value = line.partition("=")
            assert value.strip() == "", f"{line} must be blank in .env.example"
